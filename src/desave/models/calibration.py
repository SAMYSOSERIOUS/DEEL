"""Make the forecast range honest.

The range models aim for 90%, but in practice too few real hours fell inside
(about 76-84%). This looks at the last weeks of saved forecasts and finds how
much the range must be widened so that about 90% of real hours would have
fallen inside it. That factor is then applied to every new forecast.

Simple and transparent: one number, recomputed every morning, shown on the page.
"""
from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from ..config import Settings

log = logging.getLogger(__name__)

TARGET = 0.90
MIN_DAYS = 7
WINDOW_DAYS = 60
SCALES = np.round(np.arange(1.0, 4.001, 0.05), 2)


def compute(forecasts: pd.DataFrame, load: pd.Series, window_days: int = WINDOW_DAYS) -> dict:
    """Smallest widening factor (≥1) that would have put ≥90% of recent hours inside the range."""
    if forecasts.empty or not {"forecast_mwh", "q05_mwh", "q95_mwh"} <= set(forecasts.columns):
        return {"scale": 1.0, "days_used": 0}
    fc = forecasts.copy()
    # forecasts saved before calibration existed only have the raw range
    for q in ("q05", "q95"):
        raw = f"{q}_raw_mwh"
        fc[raw] = fc[raw].fillna(fc[f"{q}_mwh"]) if raw in fc else fc[f"{q}_mwh"]
    fc["actual"] = load.reindex(fc.index).to_numpy()
    fc = fc.dropna(subset=["actual", "forecast_mwh", "q05_raw_mwh", "q95_raw_mwh"])
    if fc.empty:
        return {"scale": 1.0, "days_used": 0}
    # real forecasts first: backfilled days used measured weather, so they look too good
    if "backfilled" in fc and fc.loc[~fc["backfilled"].astype(bool), "target_date"].nunique() >= MIN_DAYS:
        fc = fc[~fc["backfilled"].astype(bool)]
    days = sorted(fc["target_date"].unique())[-window_days:]
    fc = fc[fc["target_date"].isin(days)]
    if len(days) < MIN_DAYS:
        return {"scale": 1.0, "days_used": int(len(days))}
    f, a = fc["forecast_mwh"].to_numpy(), fc["actual"].to_numpy()
    below = np.maximum(f - fc["q05_raw_mwh"].to_numpy(), 1e-9)
    above = np.maximum(fc["q95_raw_mwh"].to_numpy() - f, 1e-9)
    err = a - f

    def coverage(s: float) -> float:
        return float(np.mean((err >= -s * below) & (err <= s * above)))

    scale = next((float(s) for s in SCALES if coverage(s) >= TARGET), float(SCALES[-1]))
    return {"scale": scale, "days_used": int(len(days)), "hours_used": int(len(fc)),
            "coverage_before": round(100 * coverage(1.0), 1), "coverage_after": round(100 * coverage(scale), 1),
            "target": int(TARGET * 100)}


def apply(df: pd.DataFrame, scale: float) -> pd.DataFrame:
    """Widen the range around the forecast; keep the raw range too, for the next calibration."""
    out = df.copy()
    out["q05_raw_mwh"], out["q95_raw_mwh"] = df["q05_mwh"], df["q95_mwh"]
    f = df["forecast_mwh"]
    out["q05_mwh"] = f - scale * (f - df["q05_mwh"])
    out["q95_mwh"] = f + scale * (df["q95_mwh"] - f)
    out["range_scale"] = scale
    return out


def path(cfg: Settings):
    return cfg.path("results") / "interval_calibration.json"


def save(cfg: Settings, info: dict) -> None:
    info = dict(info, computed_utc=pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"))
    path(cfg).write_text(json.dumps(info, indent=2))


def load_scale(cfg: Settings) -> float:
    try:
        return float(json.loads(path(cfg).read_text()).get("scale", 1.0))
    except (FileNotFoundError, ValueError):
        return 1.0
