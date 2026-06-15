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


def db_url() -> str:
    return os.environ.get("DASHBOARD_DATABASE_URL") or f"sqlite:///{DEFAULT_SQLITE.as_posix()}"


def _engine():
    return create_engine(db_url())


def table(name: str) -> pd.DataFrame:
    """Read a curated table with a short TTL cache. Empty DataFrame if missing (never raises)."""
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < TTL_S:
        return hit[1]
    try:
        df = pd.read_sql_table(name, _engine())
    except Exception:
        df = pd.DataFrame()
    _cache[name] = (now, df)
    return df


def meta_value(key: str, default: str = "—") -> str:
    df = table("meta")
    if df.empty or "key" not in df:
        return default
    row = df[df["key"] == key]
    return str(row["value"].iloc[0]) if not row.empty else default
