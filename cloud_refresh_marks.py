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

import gzip
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
RESCURVE_CAP_ROWS = 16000         # preserve restored per-resolution 5-min history across the full $1k test
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


def _append_rows(eng, name: str, cols: list[str], rows: list[dict], cap: int,
                 order_col: str = "ts") -> str:
    """EGRESS-LEAN append: INSERT the new rows + occasional prune beyond cap. Replaces the
    read-whole-table -> rewrite-whole-table pattern that pulled the full 8k/16k-row history from Neon
    EVERY 3-min cycle (~1.5GB/day egress) and exhausted the free-tier data-transfer quota
    (HTTP 402, 2026-07-08). Tolerates legacy schemas (intersects columns) and creates the table on
    first run. Never raises; returns a short status."""
    if not rows:
        return "no_rows"

    def _insert(use_cols):
        collist = ", ".join(f'"{c}"' for c in use_cols)
        binds = ", ".join(f":{c}" for c in use_cols)
        params = [{c: r.get(c) for c in use_cols} for r in rows]
        with eng.begin() as cx:
            cx.execute(text(f'INSERT INTO "{name}" ({collist}) VALUES ({binds})'), params)
    try:
        _insert(cols)
        status = "appended"
    except Exception:
        try:                                   # legacy table w/ fewer columns? intersect and retry
            have = list(pd.read_sql(f'SELECT * FROM "{name}" LIMIT 0', eng).columns)
            use = [c for c in cols if c in have]
            if use:
                _insert(use)
                status = f"appended({len(use)}/{len(cols)} cols)"
            else:
                return "error:no_common_cols"
        except Exception:
            try:                               # table does not exist yet -> create it
                pd.DataFrame(rows)[cols].to_sql(name, eng, if_exists="append", index=False)
                status = "created"
            except Exception as exc:           # noqa: BLE001 -- sanitized, never the DSN
                return f"error:{type(exc).__name__}"
    try:                                       # prune ~1/50 cycles (keeps the cap without hot egress)
        import random
        if random.random() < 0.02:
            with eng.begin() as cx:
                cx.exec_driver_sql(
                    f'DELETE FROM "{name}" WHERE "{order_col}" NOT IN '
                    f'(SELECT "{order_col}" FROM "{name}" ORDER BY "{order_col}" DESC LIMIT {int(cap)})')
            status += "+pruned"
    except Exception:
        pass
    return status


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


SPEC_RAW_URL = "https://raw.githubusercontent.com/RealGB80/AeroAlpha/cloud-spec/cloud_marks_spec.json"
# ---- NEON-FREE LIVE STORE (2026-07-09/10): publish the 5 live-mark tables to the `live-marks` branch
# every cycle (amend+force, single writer = this job; laptop owns `cloud-spec` separately, no clash).
# The app reads the raw file git-first (data.py), so the site no longer depends on Neon quotas at all.
# Equity/rescurve history rides in a LOCAL state file across loop iterations (seeded from the branch on
# job start, Neon fallback), so no full-table reads anywhere. Neon writes stay as best-effort fallback.
LIVE_BRANCH = "live-marks"
LIVE_FILE = "live_marks.json.gz"
LIVE_RAW_URL = f"https://raw.githubusercontent.com/RealGB80/AeroAlpha/{LIVE_BRANCH}/{LIVE_FILE}"
_STATE_PATH = "live_marks_state.json.gz"        # persists across the 180s loop iterations within a job
_LIVE_WT = ".live_marks_wt"


def _df_records(df) -> list:
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for r in df.to_dict("records"):
        out.append({k: (None if (isinstance(v, float) and pd.isna(v)) else
                        (v.item() if hasattr(v, "item") else v)) for k, v in r.items()})
    return out


_HIST_TABLES = ("bankroll_equity_timeline", "resolution_day_curve")
_HIST_KEYS = {"bankroll_equity_timeline": ("ts",),
              "resolution_day_curve": ("resolution_date", "ts")}
SEED_PATH = "live_marks_seed.json.gz"   # committed on MAIN (fresh in every job checkout) = durable floor


def _unwrap(blob: dict) -> dict:
    """Normalize a state/blob dict to flat {table: rows}. THE 2026-07-12 WIPE: the state file is written
    in blob shape ({'tables': {...}}) but was read back UN-unwrapped, so refresh() saw empty history every
    cycle and force-pushed ~1-row blobs over the full history 3 minutes after each job start."""
    t = blob.get("tables") if isinstance(blob.get("tables"), dict) else blob
    return {n: (t.get(n) or []) for n in _HIST_TABLES}


HIST_FLOOR = "2026-07-16"   # $1k run RESET 2026-07-13 to $1,000 under the exec-gate changepoint. Bumped
# 07-15->07-16 (2026-07-16): the 07-15 marks were a bimodal +9/+25 unrealized SAWTOOTH (missed-quote
# revert-to-entry bug, fixed above via prev_mark carry-forward) -- buggy display data, dropped from view. The
#                             07-13/07-14 equity rows were CONTAMINATED: pre-reset positions carried into the
#                             fresh book were marked underwater, dropping equity to a FALSE ~$844 (-15% cliff
#                             in the All view). The producer now excludes pre-reset signals (build_open_positions
#                             RESET HYGIENE); this floor drops the already-stored contaminated 07-13/07-14 rows
#                             so the chart is a clean forward track from the reset baseline. (Prior floors:
#                             06-26 bootstrap artifact, then 07-13 reset; both superseded.)


def _union_rows(base: list, extra: list, keys: tuple) -> list:
    """Order-stable union of row dicts deduped on `keys`, sorted by ts. History can only ever GROW --
    except rows before HIST_FLOOR (pre-run bootstrap artifact) are permanently excluded."""
    seen = {}
    for r in list(base or []) + list(extra or []):
        if isinstance(r, dict) and str(r.get("ts") or "")[:10] >= HIST_FLOOR:
            seen.setdefault(tuple(str(r.get(k)) for k in keys), r)
    return sorted(seen.values(), key=lambda r: str(r.get("ts") or ""))


def _read_blob_file(path: str) -> dict | None:
    try:
        with open(path, "rb") as fh:
            return json.loads(gzip.decompress(fh.read()).decode("utf-8"))
    except Exception:
        return None


def _load_live_state(eng) -> dict:
    """History for the two append tables: local state file (same job) -> live-marks raw (previous job)
    -> Neon (legacy fallback). The MAIN-committed seed file is ALWAYS unioned in as a floor, so even a
    total state+branch loss self-heals to at least the seed. Never raises."""
    state = None
    b = _read_blob_file(_STATE_PATH)
    if b is not None:
        state = _unwrap(b)
    if state is None:
        try:
            req = urllib.request.Request(LIVE_RAW_URL, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                state = _unwrap(json.loads(gzip.decompress(resp.read()).decode("utf-8")))
        except Exception:
            state = None
    if state is None:
        state = {n: _df_records(_read_table(eng, n)) for n in _HIST_TABLES}
    seed = _read_blob_file(SEED_PATH)
    if seed is not None:
        s = _unwrap(seed)
        for n in _HIST_TABLES:
            state[n] = _union_rows(state.get(n), s.get(n), _HIST_KEYS[n])
    return state


def _publish_live_marks(state: dict, op, pend, bmk, now: str) -> str:
    """Write the full 5-table live blob to the local state file + the live-marks branch (amend+force,
    worktree shares the checkout's auth). Returns a short status; never raises."""
    import subprocess
    blob = {"generated_utc": now, "tables": {
        "bankroll_equity_timeline": state.get("bankroll_equity_timeline") or [],
        "resolution_day_curve": state.get("resolution_day_curve") or [],
        "open_positions": _df_records(op), "pending_price_daily": _df_records(pend),
        "bankroll_marks": _df_records(bmk)}}
    payload = gzip.compress(json.dumps(blob, separators=(",", ":"), default=str).encode("utf-8"))
    try:
        with open(_STATE_PATH, "wb") as fh:
            fh.write(payload)
    except Exception:
        pass

    def run(args, cwd="."):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=120)
    try:
        if not os.path.exists(os.path.join(_LIVE_WT, ".git")):
            run(["git", "worktree", "prune"])
            run(["git", "fetch", "origin", LIVE_BRANCH])
            r = run(["git", "worktree", "add", "-B", LIVE_BRANCH, _LIVE_WT, f"origin/{LIVE_BRANCH}"])
            if r.returncode != 0:                    # branch does not exist yet -> orphan
                run(["git", "worktree", "add", "--detach", _LIVE_WT])
                run(["git", "checkout", "--orphan", LIVE_BRANCH], _LIVE_WT)
                run(["git", "rm", "-rf", "."], _LIVE_WT)
        # RATCHET GUARD (2026-07-12 wipe): union the branch's existing history into the outgoing blob
        # before overwriting -- a shrunken/empty state can then never erase history, only re-grow it.
        prev = _read_blob_file(os.path.join(_LIVE_WT, LIVE_FILE))
        if prev is not None:
            p = _unwrap(prev)
            grew = False
            for n in _HIST_TABLES:
                merged = _union_rows(blob["tables"].get(n), p.get(n), _HIST_KEYS[n])
                if len(merged) > len(blob["tables"].get(n) or []):
                    blob["tables"][n] = merged
                    grew = True
            if grew:                                  # re-serialize + refresh the state file too
                payload = gzip.compress(json.dumps(blob, separators=(",", ":"), default=str).encode("utf-8"))
                try:
                    with open(_STATE_PATH, "wb") as fh:
                        fh.write(payload)
                except Exception:
                    pass
        with open(os.path.join(_LIVE_WT, LIVE_FILE), "wb") as fh:
            fh.write(payload)
        run(["git", "add", "-A"], _LIVE_WT)
        ident = ["-c", "user.email=bot@aeroalpha.local", "-c", "user.name=aeroalpha-marks"]
        has_head = run(["git", "rev-parse", "-q", "--verify", "HEAD"], _LIVE_WT).returncode == 0
        if has_head:
            run(["git", *ident, "commit", "--amend", "-m", f"marks {now}"], _LIVE_WT)
        else:
            run(["git", *ident, "commit", "-m", f"marks {now}"], _LIVE_WT)
        p = run(["git", "-c", "core.hooksPath=.git/x-no-hooks", "push", "--force", "origin",
                 f"{LIVE_BRANCH}:{LIVE_BRANCH}"], _LIVE_WT)
        return "git_ok" if p.returncode == 0 else f"git_push_fail({p.returncode})"
    except Exception as exc:                          # noqa: BLE001
        return f"git_err:{type(exc).__name__}"
TABLES_RAW_URL = "https://raw.githubusercontent.com/RealGB80/AeroAlpha/cloud-spec/dashboard_tables.json.gz"
# Tables THIS job owns (append/rebuild here) -- NEVER overwritten by the producer-tables sync below.
_CLOUD_OWNED = {"open_positions", "bankroll_equity_timeline", "pending_price_daily",
                "bankroll_marks", "resolution_day_curve", "cloud_marks_spec"}


def _sync_producer_tables(eng) -> None:
    """Load the laptop's producer tables from the cloud-spec branch (HTTPS/443) into Neon. WHY: on networks
    that block Postgres:5432 (campus wifi, 2026-07-07) the laptop's hourly --prod write dies, freezing every
    non-live table. The laptop now ships ALL producer tables in dashboard_tables.json.gz on the same data
    branch; this job (which CAN reach Neon) writes them. Marker table `producer_tables_meta.generated_utc`
    makes it a no-op ~19 of 20 cycles (blob changes ~hourly, loop runs ~3-min). Never raises; never touches
    the cloud-owned live-mark tables."""
    try:
        req = urllib.request.Request(TABLES_RAW_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            blob = json.loads(gzip.decompress(resp.read()).decode("utf-8"))
        gen = str(blob.get("generated_utc") or "")
        tables = blob.get("tables") or {}
        if not gen or not tables:
            return
        meta = _read_table(eng, "producer_tables_meta")
        if not meta.empty and str(meta.iloc[-1].get("generated_utc")) == gen:
            return                                   # already loaded this build
        n_w = n_skip = 0
        for name, recs in tables.items():
            if name in _CLOUD_OWNED or not isinstance(recs, list) or not recs:
                n_skip += 1
                continue
            status = safe_write_table(eng, name, pd.DataFrame(recs))
            n_w += 1 if status == "written" else 0
        safe_write_table(eng, "producer_tables_meta", pd.DataFrame([{"generated_utc": gen}]))
        print(f"[cloud-marks] producer tables synced from git: {n_w} written, {n_skip} skipped (gen={gen})")
    except Exception as exc:                          # noqa: BLE001 -- tables sync must never break the marks
        print(f"[cloud-marks] producer tables sync skipped ({type(exc).__name__})")


def _load_spec(eng):
    """Load the marks spec. PRIMARY = the `cloud-spec` branch raw file over HTTPS/443 -- the laptop publishes
    there even when its network blocks Postgres:5432 (campus wifi froze the dashboard 2026-07-07). FALLBACK =
    the Neon `cloud_marks_spec` table (used when they're on an unblocked network / git fetch fails). Returns
    (spec_dict, source_str) or (None, None)."""
    try:
        import urllib.request
        req = urllib.request.Request(SPEC_RAW_URL, headers={"Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            blob = json.loads(resp.read().decode("utf-8"))
        return json.loads(blob["spec_json"]), "git"
    except Exception as exc:                       # noqa: BLE001
        print(f"[cloud-marks] git spec fetch failed ({type(exc).__name__}); falling back to Neon")
    spec_df = _read_table(eng, SPEC_TABLE)
    if spec_df.empty:
        return None, None
    return json.loads(spec_df.sort_values("generated_utc").iloc[-1]["spec_json"]), "neon"


def refresh(eng) -> int:
    _sync_producer_tables(eng)                      # 443-shipped producer tables (no-op when unchanged)
    spec, spec_src = _load_spec(eng)
    if spec is None:
        print("[cloud-marks] no cloud_marks_spec (git+Neon) -> nothing to do.")
        return 0
    print(f"[cloud-marks] spec source={spec_src} generated={spec.get('generated_utc', '?')}")
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
    prev_mark = {}                                    # ticker -> LAST CLOUD MARK (current_price)
    if not prev_op.empty and "ticker" in prev_op.columns:
        for _, _r in prev_op.iterrows():
            if "price_series" in prev_op.columns:
                prev_series[_r.get("ticker")] = _parse_series(_r.get("price_series"))
            _pm = _num(_r.get("current_price"))       # so a missed quote holds the real last mark,
            if _pm is not None:                       # not the stale spec entry (which caused the
                prev_mark[_r.get("ticker")] = _pm     # bimodal +9/+25 unrealized SAWTOOTH, 2026-07-16)

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
            cur = prev_mark.get(r.get("ticker"))    # forward-fill the LAST CLOUD MARK first (stable),
            if cur is None:                          # only then the spec's current_price as a last resort
                cur = _num(r.get("current_price"))   # (prevents the missed-quote revert-to-entry sawtooth)
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

        in_book = bool(r.get("in_1k_book")) and contracts > 0 and entry_c is not None
        # Equity (headline $) stays gated on a live/forward-filled mark -- unchanged, cent-validated formula.
        if in_book and cur is not None:
            unreal += contracts * (cur - entry_c) / 100.0
            cost_open += contracts * entry_c / 100.0
            pos_value += contracts * cur / 100.0
            n_open += 1
        # Resolution-day COST BASIS must NOT depend on a live quote arriving this run: a thin/just-opened
        # position with no mark was dropping out of `paid`, so the paid line jumped/broke (e.g. 7/5). Count
        # paid for every held in-book contract; value uses the mark when we have one, else cost (delta 0, so
        # resolution-day NET is unchanged and stays consistent with equity's unrealized).
        if in_book:
            res = str(r.get("target_date") or r.get("resolution_date") or "")[:10]
            if res:
                val_c = cur if cur is not None else entry_c
                a = res_acc.setdefault(res, {"paid": 0.0, "value": 0.0, "n": 0, "ct": 0.0})
                a["paid"] += contracts * entry_c
                a["value"] += contracts * val_c
                a["n"] += 1
                a["ct"] += contracts

    equity = round(bankroll_start + realized + unreal, 2)
    cash = round(bankroll_start + realized - cost_open, 2)

    # ---- APPEND the fresh point to the equity timeline (SQL INSERT; no full-history read) ----
    et_cols = ["ts", "equity", "realized", "unrealized", "n_open", "cash", "positions"]
    new_eq = {"ts": now, "equity": equity, "realized": round(realized, 2),
              "unrealized": round(unreal, 2), "n_open": n_open, "cash": cash,
              "positions": round(pos_value, 2)}
    et_status = _append_rows(eng, "bankroll_equity_timeline", et_cols, [new_eq], EQUITY_CAP_ROWS)

    # ---- APPEND resolution-day points (one per open resolution date; SQL INSERT, no full read) ----
    rc_cols = ["resolution_date", "ts", "cumulative_paid_c", "cumulative_value_c", "n_entered", "n_contracts"]
    add = [{"resolution_date": d, "ts": now, "cumulative_paid_c": round(a["paid"], 2),
            "cumulative_value_c": round(a["value"], 2), "n_entered": a["n"],
            "n_contracts": round(a["ct"], 2)} for d, a in sorted(res_acc.items())]
    rc_status = _append_rows(eng, "resolution_day_curve", rc_cols, add, RESCURVE_CAP_ROWS)

    # PENDING-LIST = FUNDED positions only: drop the $0 rows (watch streams + in-book-but-unfunded cold
    # candidates) that rendered as "0 contracts / -- paid" and cluttered the pending view. They are tracked in
    # the spec/gates, not held by the $1k run. equity/resolution above already count only contracts>0, so this
    # is a display filter (no P&L change).
    funded_rows = [r for r in new_rows if (_num(r.get("contracts"), 0.0) or 0.0) > 0]
    op = pd.DataFrame(funded_rows)
    pend = pd.DataFrame(spec.get("pending_price_daily") or [])
    bmk = pd.DataFrame(spec.get("bankroll_marks") or [])
    # (the old et/rc desync guard is gone: the single live-marks blob keeps all 5 tables in lockstep
    #  by construction -- one atomic file per cycle)

    # ---- NEON-FREE publish: full 5-table blob -> live-marks branch (the app reads it git-first) ----
    state = _load_live_state(eng)
    state["bankroll_equity_timeline"] = ((state.get("bankroll_equity_timeline") or [])
                                         + [new_eq])[-EQUITY_CAP_ROWS:]
    state["resolution_day_curve"] = ((state.get("resolution_day_curve") or [])
                                     + add)[-RESCURVE_CAP_ROWS:]
    live_status = _publish_live_marks(state, op, pend, bmk, now)

    results = {"live_marks_blob": f"{live_status} (et={len(state['bankroll_equity_timeline'])}, "
                                  f"rc={len(state['resolution_day_curve'])} rows)",
               "bankroll_equity_timeline": f"neon:{et_status} (+1 row)",
               "resolution_day_curve": f"neon:{rc_status} (+{len(add)} rows)"}
    rowcounts = {"open_positions": len(op), "pending_price_daily": len(pend), "bankroll_marks": len(bmk)}
    for name, df in (("open_positions", op), ("pending_price_daily", pend), ("bankroll_marks", bmk)):
        results[name] = f"{safe_write_table(eng, name, df)} ({rowcounts[name]} rows)"
    print(f"[cloud-marks] equity=${equity:,.2f} (realized {realized:+.2f} / unreal {unreal:+.2f}, {n_open} open) "
          f"| quotes {n_fresh}/{len(tickers)} in {pull_s:.1f}s | spec_age={spec.get('generated_utc')}")
    for k, v in results.items():
        print(f"    {k}: [{v}]")
    err = [k for k, v in results.items() if "error" in v]
    return 1 if err else 0


def main() -> int:
    eng = make_engine()
    try:
        return refresh(eng)
    finally:
        eng.dispose()


if __name__ == "__main__":
    sys.exit(main())
