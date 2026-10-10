"""Simple storage: one Parquet file per dataset per month (by UTC month).

Why monthly files? The daily job only touches the current month's file, so a
git repository holding the data grows by a small amount each day instead of
re-saving the whole history.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import Settings


def _dir(cfg: Settings, name: str) -> Path:
    d = cfg.path("data") / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_monthly(cfg: Settings, name: str, df: pd.DataFrame) -> list[Path]:
    """Merge ``df`` (UTC time index) into the stored dataset. New values replace old ones."""
    if df.empty:
        return []
    if df.index.tz is None:
        raise ValueError("save_monthly needs a UTC time index")
    df = df.copy()
    df.index = df.index.tz_convert("UTC")
    df.index.name = "time_utc"
    written = []
    months = df.index.tz_localize(None).to_period("M")
    for period, part in df.groupby(months):
        f = _dir(cfg, name) / f"{period}.parquet"
        if f.exists():
            old = pd.read_parquet(f)
            part = pd.concat([old[~old.index.isin(part.index)], part]).sort_index()
            # keep columns from both; new values win where present
        part = part[~part.index.duplicated(keep="last")].sort_index()
        part.to_parquet(f)
        written.append(f)
    return written


def load(cfg: Settings, name: str, start: str | pd.Timestamp | None = None,
         end: str | pd.Timestamp | None = None) -> pd.DataFrame:
    d = _dir(cfg, name)
    files = sorted(d.glob("*.parquet"))
    if not files:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="time_utc"))
    if start is not None or end is not None:
        s = pd.Timestamp(start).tz_localize(None).to_period("M") if start is not None else None
        e = pd.Timestamp(end).tz_localize(None).to_period("M") if end is not None else None
        keep = []
        for f in files:
            p = pd.Period(f.stem, freq="M")
            if (s is None or p >= s - 1) and (e is None or p <= e + 1):
                keep.append(f)
        files = keep
    if not files:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC", name="time_utc"))
    df = pd.concat([pd.read_parquet(f) for f in files]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if start is not None:
        df = df[df.index >= _utc(start)]
    if end is not None:
        df = df[df.index < _utc(end)]
    return df


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tz is None else t.tz_convert("UTC")


def last_time(cfg: Settings, name: str) -> pd.Timestamp | None:
    files = sorted(_dir(cfg, name).glob("*.parquet"))
    if not files:
        return None
    df = pd.read_parquet(files[-1])
    return None if df.empty else df.index.max()
