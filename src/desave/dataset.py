"""Load the stored data in the shape the models need."""
from __future__ import annotations

import logging

import pandas as pd

from . import store, validate
from .config import Settings
from .sources import dwd

log = logging.getLogger(__name__)


def load_series(cfg: Settings, check: bool = True, start=None, end=None) -> pd.Series:
    """Hourly grid load in MWh (UTC), checked and with short gaps filled."""
    df = store.load(cfg, cfg.load_store, start, end)
    if df.empty:
        raise FileNotFoundError(f"No demand data for {cfg.area} yet. Run: desave download")
    s = df["load_mwh"].astype("float64").dropna()
    if check:
        bounds = validate.PLAUSIBLE_MWH if cfg.area == "DE" else validate.PLAUSIBLE_REGION_MWH
        rep = validate.check_load(s, cfg.tz, plausible=bounds)
        for w in rep.warnings:
            log.warning(w)
        rep.raise_if_failed()
    return validate.fill_short_gaps(s).rename("load_mwh")


def weather_observed(cfg: Settings, start=None, end=None) -> pd.DataFrame:
    df = store.load(cfg, dwd.NAME, start, end)
    if df.empty:
        raise FileNotFoundError("No weather data yet. Run: desave download")
    return pd.DataFrame({"temp": dwd.national(df, cfg, "temp"), "sun": dwd.national(df, cfg, "sun")})


def weather_with_forecast(cfg: Settings, forecast_wide: pd.DataFrame | None = None, start=None) -> pd.DataFrame:
    """Observed weather, continued with forecast values after the last observation."""
    obs = weather_observed(cfg, start=start)
    obs = obs.dropna(subset=["temp"])
    if forecast_wide is None:
        forecast_wide = store.load(cfg, "weather_fc", start=obs.index.max() - pd.Timedelta(days=3))
    if forecast_wide is None or forecast_wide.empty:
        return obs
    fc = pd.DataFrame({"temp": dwd.national(forecast_wide, cfg, "temp"),
                       "sun": dwd.national(forecast_wide, cfg, "sun")})
    fc = fc[fc.index > obs.index.max()]
    return pd.concat([obs, fc]).sort_index()
