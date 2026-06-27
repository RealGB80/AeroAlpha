"""
CLOUD live-marks refresher -- runs on GitHub Actions cron (always-on, laptop-INDEPENDENT). PAPER ONLY.

WHY THIS EXISTS
  The $1k dashboard marks used to freeze whenever the producer laptop slept (Windows Modern Standby -> no
  scheduled tasks run). This job moves the every-few-minutes MARK refresh into the cloud so freshness no longer
  depends on the laptop being awake. Each run:
    1. reads the SPEC the laptop published to Neon (`cloud_marks_spec`): which positions are open, their entry
       price + EXACT harness contracts, the realized P&L, and pass-through pending/marks tables.
    2. re-pulls the current PUBLIC Kalshi quote for each open ticker (unauth GET /markets/{ticker}), bounded by
       a hard wall-clock deadline so a slow API can never hang the job (missing tickers keep their last mark).
    3. APPENDS a fresh point to bankroll_equity_timeline + resolution_day_curve, rebuilds open_positions with
       the new marks, and writes the 5 live tables back to Neon via an ATOMIC, skip-on-empty swap.
  equity = bankroll_start + realized + unrealized, where unrealized = sum(contracts * (mark - entry)/100) on the
  side held -- the IDENTICAL formula the laptop uses (validated equal to the cent).

HARD BOUNDARIES: paper only; NO auth, NO orders, NO account endpoints, NO real money, NO P&L claim. Public
  unauth GET only. DASHBOARD_DATABASE_URL from the Actions secret (env), never hardcoded; errors are reported by
  EXCEPTION TYPE only (a SQLAlchemy error can echo the postgresql://user:pass@host credential). ASCII stdout.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, text

KALSHI_BASE = "https://external-api.kalshi.com/trade-api/v2"
SPEC_TABLE = "cloud_marks_spec"
EQUITY_CAP_ROWS = 8000          # bound table growth (~28 days at a 4-min cadence); the chart downsamples anyway
RESCURVE_CAP_ROWS = 4000
SPARK_CAP = 60                  # per-position sparkline length
PULL_BUDGET_S = 90.0            # hard cap on the whole quote pull -> the job always finishes and writes
MAX_WORKERS = 12


# --------------------------------------------------------------------------- store
def make_engine():
    url = os.environ.get("DASHBOARD_DATABASE_URL")
    if not url:
        raise SystemExit("[cloud-marks] DASHBOARD_DATABASE_URL not set (GitHub Actions secret missing).")
    return create_engine(
        url,
        connect_args={"connect_timeout": 15, "options": "-c statement_timeout=30000"},
        pool_pre_ping=True, pool_recycle=300,
    )


def _table_rowcount(eng, name: str) -> int:
    try:
        with eng.connect() as cx:
            return int(cx.exec_driver_sql(f'SELECT COUNT(*) FROM "{name}"').scalar() or 0)
    except Exception:
        return 0


def safe_write_table(eng, name: str, df: pd.DataFrame) -> str:
    """ATOMIC + SKIP-ON-EMPTY + NO-CRED-IN-LOG publish (mirrors the producer's safe_write_table)."""
    try:
        if (df is None or df.empty) and _table_rowcount(eng, name) > 0:
            return "skipped_empty"
        tmp = f"{name}__tmp"
        df.to_sql(tmp, eng, if_exists="replace", index=False)
        with eng.begin() as cx:
            cx.exec_driver_sql(f'DROP TABLE IF EXISTS "{name}"')
            cx.exec_driver_sql(f'ALTER TABLE "{tmp}" RENAME TO "{name}"')
        return "written"
    except Exception as exc:                      # noqa: BLE001 -- never surface the connection string
        return f"error:{type(exc).__name__}"


def _read_table(eng, name: str) -> pd.DataFrame:
    try:
        return pd.read_sql(f'SELECT * FROM "{name}"', eng)
    except Exception:
        return pd.DataFrame()


# --------------------------------------------------------------------------- quotes
def fetch_yes_mid_cents(ticker: str):
    """Public unauth GET /markets/{ticker} -> YES mid (cents) or None. IDENTICAL mid logic to the producer."""
    if not ticker:
        return None
    try:
        req = urllib.request.Request(f"{KALSHI_BASE}/markets/{ticker}",
                                     headers={"User-Agent": "kxhighny-paper-marks/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            if resp.status != 200:
                return None
            m = (json.loads(resp.read().decode("utf-8")) or {}).get("market") or {}
    except Exception:
        return None

    def _mid(bid, ask, scale):
        if bid is None or ask is None:
            return None
        b, a = float(bid), float(ask)
        if b <= 0.0 and a >= scale:                 # degenerate empty/full-width book
            return None
        if a - b >= scale * 0.99:
            return None
        return (b + a) / 2.0 * (100.0 / scale)

    mid = _mid(m.get("yes_bid"), m.get("yes_ask"), 100.0)
    if mid is None:
        mid = _mid(m.get("yes_bid_dollars"), m.get("yes_ask_dollars"), 1.0)
    if mid is not None:
        return mid
    lp = m.get("last_price")
    if lp is not None:
        return float(lp)
    lpd = m.get("last_price_dollars")
    if lpd is not None:
        return float(lpd) * 100.0
    return None


def pull_quotes(tickers: list[str]) -> dict:
    """Deadline-bounded parallel pull. Tickers unresolved at the budget stay absent -> caller forward-fills."""
    out: dict[str, float] = {}
    if not tickers:
        return out
    ex = ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(tickers)))
    futs = {ex.submit(fetch_yes_mid_cents, t): t for t in tickers}
    try:
        for fut in as_completed(list(futs), timeout=PULL_BUDGET_S):
            v = fut.result()
            if v is not None:
                out[futs[fut]] = float(v)
    except Exception:
        pass                                        # deadline reached -> use whatever resolved
    ex.shutdown(wait=False, cancel_futures=True)
    return out


# --------------------------------------------------------------------------- marking
def _num(v, default=None):
    try:
        if v is None:
            return default
        f = float(v)
        return default if f != f else f            # NaN guard
    except (TypeError, ValueError):
        return default


def refresh(eng) -> int:
    spec_df = _read_table(eng, SPEC_TABLE)
    if spec_df.empty:
        print("[cloud-marks] no cloud_marks_spec yet (laptop has not published) -> nothing to do.")
        return 0
    spec = json.loads(spec_df.sort_values("generated_utc").iloc[-1]["spec_json"])
    bankroll_start = float(spec.get("bankroll_start") or 1000.0)
    realized = float(spec.get("realized") or 0.0)
    positions = spec.get("positions") or []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    tickers = sorted({p.get("ticker") for p in positions if p.get("ticker")})
    t0 = time.time()
    quotes = pull_quotes(tickers)
    pull_s = time.time() - t0

    # Carry each position's price sparkline forward from THIS table's own last cloud write (so it GROWS across
    # runs, not just within one static spec). price_series is stored the SAME way the producer stores it -- a
    # json.dumps(list) STRING -- which the app parses with json.loads; reset-to-list broke the trend to one dot.
    def _parse_series(v):
        try:
            seq = json.loads(v) if isinstance(v, str) else (list(v) if v else [])
        except (ValueError, TypeError):
            seq = []
        return [float(x) for x in seq if x is not None]

    prev_op = _read_table(eng, "open_positions")
    prev_series = {}
    if not prev_op.empty and "ticker" in prev_op.columns and "price_series" in prev_op.columns:
        for _, _r in prev_op.iterrows():
            prev_series[_r.get("ticker")] = _parse_series(_r.get("price_series"))

    # ---- re-mark each position (fresh quote on the side held; forward-fill last mark if the quote missed) ----
    unreal = 0.0
    cost_open = 0.0
    pos_value = 0.0
    n_open = 0
    new_rows = []
    res_acc: dict[str, dict] = {}
    n_fresh = 0
    for p in positions:
        r = dict(p)
        side = str(r.get("side") or "YES").upper()
        entry_c = _num(r.get("entry_price"))
        contracts = _num(r.get("contracts"), 0.0) or 0.0
        ymid = quotes.get(r.get("ticker"))
        if ymid is not None:
            cur = (100.0 - ymid) if side == "NO" else ymid
            n_fresh += 1
        else:
            cur = _num(r.get("current_price"))      # forward-fill last known mark
        r["current_price"] = None if cur is None else round(cur, 2)
        if entry_c is not None and cur is not None:
            r["mark_delta"] = round(cur - entry_c, 2)
            r["direction"] = "up" if cur > entry_c else ("down" if cur < entry_c else "flat")
        # grow the per-position sparkline: base = this table's own last series (accumulates), else the spec's
        # producer-built series, else seed from entry. Append the fresh mark; store as a json STRING (app format).
        base = prev_series.get(r.get("ticker"))
        if base is None:
            base = _parse_series(r.get("price_series"))
            if not base and entry_c is not None:
                base = [round(entry_c, 2)]
        if cur is not None:
            base = (base + [round(cur, 2)])[-SPARK_CAP:]
        r["price_series"] = json.dumps(base)
        new_rows.append(r)

        if r.get("in_1k_book") and contracts > 0 and entry_c is not None and cur is not None:
            unreal += contracts * (cur - entry_c) / 100.0
            cost_open += contracts * entry_c / 100.0
            pos_value += contracts * cur / 100.0
            n_open += 1
            res = str(r.get("target_date") or r.get("resolution_date") or "")[:10]
            if res:
                a = res_acc.setdefault(res, {"paid": 0.0, "value": 0.0, "n": 0, "ct": 0.0})
                a["paid"] += contracts * entry_c
                a["value"] += contracts * cur
                a["n"] += 1
                a["ct"] += contracts

    equity = round(bankroll_start + realized + unreal, 2)
    cash = round(bankroll_start + realized - cost_open, 2)

    # ---- APPEND the fresh point to the equity timeline (read existing history from Neon, add one row) ----
    et = _read_table(eng, "bankroll_equity_timeline")
    et_cols = ["ts", "equity", "realized", "unrealized", "n_open", "cash", "positions"]
    new_eq = {"ts": now, "equity": equity, "realized": round(realized, 2),
              "unrealized": round(unreal, 2), "n_open": n_open, "cash": cash,
              "positions": round(pos_value, 2)}
    et = pd.concat([et, pd.DataFrame([new_eq])], ignore_index=True) if not et.empty else pd.DataFrame([new_eq])
    et = et[[c for c in et_cols if c in et.columns]].tail(EQUITY_CAP_ROWS)

    # ---- APPEND resolution-day points (one per open resolution date) ----
    rc = _read_table(eng, "resolution_day_curve")
    rc_cols = ["resolution_date", "ts", "cumulative_paid_c", "cumulative_value_c", "n_entered", "n_contracts"]
    add = [{"resolution_date": d, "ts": now, "cumulative_paid_c": round(a["paid"], 2),
            "cumulative_value_c": round(a["value"], 2), "n_entered": a["n"],
            "n_contracts": round(a["ct"], 2)} for d, a in sorted(res_acc.items())]
    if add:
        rc = pd.concat([rc, pd.DataFrame(add)], ignore_index=True) if not rc.empty else pd.DataFrame(add)
        rc = rc[[c for c in rc_cols if c in rc.columns]].tail(RESCURVE_CAP_ROWS)

    # PENDING-LIST = FUNDED positions only: drop the $0 rows (watch streams + in-book-but-unfunded cold
    # candidates) that rendered as "0 contracts / -- paid" and cluttered the pending view. They are tracked in
    # the spec/gates, not held by the $1k run. equity/resolution above already count only contracts>0, so this
    # is a display filter (no P&L change).
    funded_rows = [r for r in new_rows if (_num(r.get("contracts"), 0.0) or 0.0) > 0]
    op = pd.DataFrame(funded_rows)
    pend = pd.DataFrame(spec.get("pending_price_daily") or [])
    bmk = pd.DataFrame(spec.get("bankroll_marks") or [])

    # desync guard: never publish exactly one of {equity, resolution} empty (keep both prior copies in lockstep)
    if et.empty != rc.empty and not (et.empty and rc.empty):
        et, rc = pd.DataFrame(), pd.DataFrame()

    results = {}
    for name, df in (("open_positions", op), ("bankroll_equity_timeline", et),
                     ("resolution_day_curve", rc), ("pending_price_daily", pend),
                     ("bankroll_marks", bmk)):
        results[name] = safe_write_table(eng, name, df)
    rowcounts = {"open_positions": len(op), "bankroll_equity_timeline": len(et),
                 "resolution_day_curve": len(rc), "pending_price_daily": len(pend), "bankroll_marks": len(bmk)}
    print(f"[cloud-marks] equity=${equity:,.2f} (realized {realized:+.2f} / unreal {unreal:+.2f}, {n_open} open) "
          f"| quotes {n_fresh}/{len(tickers)} in {pull_s:.1f}s | spec_age={spec.get('generated_utc')}")
    for k, v in results.items():
        print(f"    {k}: {rowcounts[k]} rows [{v}]")
    err = [k for k, v in results.items() if v.startswith("error")]
    return 1 if err else 0


def main() -> int:
    eng = make_engine()
    try:
        return refresh(eng)
    finally:
        eng.dispose()


if __name__ == "__main__":
    sys.exit(main())
