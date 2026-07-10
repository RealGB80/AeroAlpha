"""Data-access layer for the investor dashboard. Reads the CURATED store only (never raw artifacts).

Engine from env DASHBOARD_DATABASE_URL (managed Postgres when hosted) else the local sqlite the
materializer writes. Same code both ways. Short TTL cache so the hosted app picks up new pipeline
pushes without a restart.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SQLITE = PROJECT_ROOT / "data" / "dashboard_app.db"
TTL_S = 120  # re-read the store at most this often

_cache: dict[str, tuple[float, pd.DataFrame]] = {}
_ENGINE = None  # PERF (2026-06-22): one POOLED engine for the whole process, NOT a fresh connect per query.


def db_url() -> str:
    return os.environ.get("DASHBOARD_DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE.as_posix()}"


def _engine():
    """A single shared, pooled engine. Creating a NEW engine per table() call (the old behaviour) meant a
    fresh TLS+auth handshake to Neon on EVERY read -> dozens of cold remote connects per page render, the
    main source of dashboard slowness. pool_pre_ping + pool_recycle keep Neon's serverless connections (which
    drop when idle) healthy without reconnecting each query."""
    global _ENGINE
    if _ENGINE is None:
        url = db_url()
        if url.startswith("sqlite"):
            _ENGINE = create_engine(url)
        else:
            _ENGINE = create_engine(url, pool_size=5, max_overflow=10, pool_pre_ping=True,
                                    pool_recycle=300, pool_timeout=10)
    return _ENGINE


# ---- GIT-FIRST data store (2026-07-09, Neon-quota independence) -------------------------------------
# WHY: Neon's free-tier DATA TRANSFER quota exhausted (HTTP 402, 2026-07-08) and blanked the whole site.
# The laptop already publishes ALL producer tables to the public repo's `cloud-spec` branch over
# git/443, and the marks job publishes its live tables to `live-marks` -- so the app can read BOTH raw
# files directly and never depend on Neon. Hosted mode is GIT-FIRST with Neon as fallback; local sqlite
# mode is untouched (no network in dev/smoke). raw CDN caches ~5min -> worst-case staleness ~8min,
# comparable to the old pipeline cadence. No credentials involved: the repo is public.
_GIT_BLOBS = (
    "https://raw.githubusercontent.com/RealGB80/AeroAlpha/cloud-spec/dashboard_tables.json.gz",
    "https://raw.githubusercontent.com/RealGB80/AeroAlpha/live-marks/live_marks.json.gz",
)
_git_store: dict = {"at": 0.0, "tables": {}}


def _git_tables() -> dict:
    """{table_name: DataFrame} refreshed from the two git blobs at most every TTL_S. Never raises;
    serves the previous snapshot on any fetch/parse failure."""
    import gzip
    import json as _json
    import urllib.request
    now = time.time()
    if now - _git_store["at"] < TTL_S and _git_store["tables"]:
        return _git_store["tables"]
    tables = {}
    ok = False
    for url in _GIT_BLOBS:
        try:
            req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                blob = _json.loads(gzip.decompress(resp.read()).decode("utf-8"))
            for name, recs in (blob.get("tables") or {}).items():
                if isinstance(recs, list) and recs:
                    tables[name] = pd.DataFrame(recs)
            ok = True
        except Exception:
            continue                                  # missing branch / network blip -> other blob still loads
    if ok and tables:
        _git_store["at"] = now
        _git_store["tables"] = tables
    return _git_store["tables"]


def table(name: str) -> pd.DataFrame:
    """Read a curated table with a short TTL cache. Empty DataFrame if missing (never raises). On a read
    error, serve the last cached copy (stale) rather than blanking the panel. Hosted (postgres URL):
    GIT-FIRST (quota-proof), Neon fallback. Local sqlite: unchanged."""
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < TTL_S:
        return hit[1]
    df = pd.DataFrame()
    if not db_url().startswith("sqlite"):
        gt = _git_tables()
        if name in gt:
            df = gt[name]
    if df.empty:
        try:
            df = pd.read_sql_table(name, _engine())
        except Exception:
            df = hit[1] if hit else pd.DataFrame()   # serve stale on a transient hiccup
    _cache[name] = (now, df)
    return df


def meta_value(key: str, default: str = "—") -> str:
    df = table("meta")
    if df.empty or "key" not in df:
        return default
    row = df[df["key"] == key]
    return str(row["value"].iloc[0]) if not row.empty else default
