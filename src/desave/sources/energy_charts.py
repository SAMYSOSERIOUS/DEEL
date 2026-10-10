"""Energy-Charts (Fraunhofer ISE): backup source for German load. No key, no sign-up.

Endpoint: {base}/public_power?country=de&start=YYYY-MM-DD&end=YYYY-MM-DD
Answer:   {"unix_seconds": [...], "production_types": [{"name": "Load", "data": [...]}, ...]}
Values are MW every 15 minutes; the average over an hour = MWh in that hour.
start/end are German calendar days (both included).

Its "Load" is defined a little differently from SMARD's grid load, so before
filling SMARD gaps we scale it to SMARD with the hours where both exist.
Source to name: Energy-Charts, Fraunhofer ISE (energy-charts.info).
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import store
from ..config import Settings
from ..http import get
from .entsoe import calibrate

log = logging.getLogger(__name__)


def parse_public_power(payload: dict, name: str = "Load") -> pd.Series:
    t = pd.to_datetime(np.asarray(payload["unix_seconds"], dtype="int64"), unit="s", utc=True)
    series = next((p for p in payload.get("production_types", []) if p.get("name") == name), None)
    if series is None:
        raise ValueError(f"Energy-Charts answer has no '{name}' series")
    vals = np.array([np.nan if v is None else v for v in series["data"]], dtype="float64")
    s = pd.Series(vals, index=pd.DatetimeIndex(t)).dropna()
    hourly = s.resample("h").mean()
    counts = s.resample("h").count()
    return hourly[counts >= 3].rename("load_mwh")  # need at least 3 of the 4 quarter-hours


def download(cfg: Settings, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    base = cfg["sources"]["energy_charts"]["base_url"]
    tz = cfg.tz
    parts = []
    day = start.tz_convert(tz).normalize()
    last = end.tz_convert(tz).normalize()
    while day <= last:
        chunk_end = min(day + pd.Timedelta(days=30), last)
        r = get(f"{base}/public_power", params={"country": "de", "start": day.strftime("%Y-%m-%d"),
                                                "end": chunk_end.strftime("%Y-%m-%d")}, timeout=120)
        parts.append(parse_public_power(r.json()))
        day = chunk_end + pd.Timedelta(days=1)
    if not parts:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.concat(parts).sort_index()
    return s[~s.index.duplicated(keep="last")]


def fill_gaps(cfg: Settings, lookback_days: int = 30) -> int:
    """Fill missing recent SMARD hours with scaled Energy-Charts values. Returns hours filled."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    hist = store.load(cfg, "load", start=now - pd.Timedelta(days=400))
    if hist.empty:
        return 0
    smard_only = hist.loc[hist.get("source", pd.Series("smard", index=hist.index)) == "smard", "load_mwh"]
    full_idx = pd.date_range(now - pd.Timedelta(days=lookback_days), now - pd.Timedelta(days=1), freq="h")
    missing = full_idx.difference(hist.index[hist["load_mwh"].notna()])
    if len(missing) == 0:
        return 0
    ec = download(cfg, now - pd.Timedelta(days=120), now)
    ratio = calibrate(smard_only, ec, days=90)
    fill = ec.reindex(missing).dropna()
    fill = fill * ratio.reindex(fill.index.hour).to_numpy()
    df = fill.to_frame("load_mwh")
    df["source"] = "energy_charts_scaled"
    store.save_monthly(cfg, "load", df)
    log.warning("Filled %d missing SMARD hours with scaled Energy-Charts data", len(df))
    return len(df)
