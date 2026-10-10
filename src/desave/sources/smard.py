"""SMARD (Bundesnetzagentur) electricity data.

Source: Bundesnetzagentur | SMARD.de, licence CC BY 4.0 (name the source).

How SMARD's data feed works (the same one the SMARD website uses):
  1. ``{base}/{filter}/{region}/index_{resolution}.json`` lists the start
     times (milliseconds, UTC) of all available chunks. For hourly data one
     chunk = one week, starting Monday 00:00 German time.
  2. ``{base}/{filter}/{region}/{filter}_{region}_{resolution}_{start}.json``
     holds ``{"meta_data": ..., "series": [[ms, value], ...]}``. Hours not
     yet published have ``null`` values.
Filter 410 = total electricity consumption (grid load), MWh per hour.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import store
from ..config import Settings
from ..http import get
from ..timeutils import utc_index_from_ms

log = logging.getLogger(__name__)
NAME = "load"


def _url(cfg: Settings, filt: int, tail: str) -> str:
    s = cfg["sources"]["smard"]
    return f"{s['base_url']}/{filt}/{s['region']}/{tail}"


def index_timestamps(cfg: Settings, filt: int | None = None) -> list[int]:
    s = cfg["sources"]["smard"]
    filt = filt or s["load_filter"]
    r = get(_url(cfg, filt, f"index_{s['resolution']}.json"))
    return sorted(int(t) for t in r.json()["timestamps"])


def parse_chunk(payload: dict) -> pd.Series:
    """Turn one SMARD chunk into a UTC-indexed series, dropping not-yet-published hours."""
    rows = [(int(t), v) for t, v in payload.get("series", []) if v is not None]
    if not rows:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    ts, vals = zip(*rows)
    return pd.Series(np.asarray(vals, dtype="float64"), index=utc_index_from_ms(ts))


def fetch_chunk(cfg: Settings, ts: int, filt: int | None = None) -> pd.Series:
    s = cfg["sources"]["smard"]
    filt = filt or s["load_filter"]
    r = get(_url(cfg, filt, f"{filt}_{s['region']}_{s['resolution']}_{ts}.json"))
    return parse_chunk(r.json())


def download(cfg: Settings, start, end=None, filt: int | None = None) -> pd.Series:
    """All hourly values from ``start`` (incl.) to ``end`` (excl.), UTC."""
    start = pd.Timestamp(start, tz="UTC") if pd.Timestamp(start).tz is None else pd.Timestamp(start).tz_convert("UTC")
    end = pd.Timestamp.now(tz="UTC") + pd.Timedelta(days=2) if end is None else (
        pd.Timestamp(end, tz="UTC") if pd.Timestamp(end).tz is None else pd.Timestamp(end).tz_convert("UTC"))
    stamps = index_timestamps(cfg, filt)
    start_ms, end_ms = start.value // 10**6, end.value // 10**6
    week_ms = 8 * 24 * 3600 * 1000  # a chunk covers one week; 8 days leaves room for the clock change
    wanted = [t for t in stamps if t < end_ms and t + week_ms > start_ms]
    log.info("SMARD: downloading %d weekly chunks", len(wanted))
    parts = [fetch_chunk(cfg, t, filt) for t in wanted]
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.concat(parts).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    return s[(s.index >= start) & (s.index < end)]


def update(cfg: Settings, full: bool = False) -> pd.DataFrame:
    """Download new (and recently corrected) hours and store them."""
    last = store.last_time(cfg, NAME)
    if full or last is None:
        start = pd.Timestamp(cfg["project"]["start"]).tz_localize(cfg.tz).tz_convert("UTC")
    else:
        start = last - pd.Timedelta(days=cfg["sources"]["smard"]["refetch_days"])
    s = download(cfg, start)
    df = s.to_frame("load_mwh")
    df["source"] = "smard"
    store.save_monthly(cfg, NAME, df)
    log.info("SMARD: stored %d hours up to %s", len(df), df.index.max() if len(df) else None)
    return df
