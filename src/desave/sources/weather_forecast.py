"""Weather forecast for the next days, for the daily demand forecast.

Main: Bright Sky (brightsky.dev), which serves the DWD's own station forecasts
(MOSMIX) as simple JSON. Backup: Open-Meteo's copy of the DWD ICON model.

Both are asked for the exact coordinates of the DWD stations we use for the
history, so forecast and history describe the same places.

Note: the model was trained on MEASURED temperatures but runs every day on
FORECAST temperatures. Forecast errors therefore add to the demand forecast
error. The web page reports the real, combined error.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import store
from ..config import Settings
from ..http import get
from . import dwd

log = logging.getLogger(__name__)
NAME = "weather_fc"


def parse_brightsky(payload: dict) -> pd.DataFrame:
    rows = payload.get("weather", [])
    if not rows:
        return pd.DataFrame(columns=["temp", "sun"])
    t = pd.to_datetime([r["timestamp"] for r in rows], utc=True)
    df = pd.DataFrame({
        "temp": [r.get("temperature") for r in rows],
        "sun": [r.get("sunshine") for r in rows],  # minutes of sun in the previous hour
    }, index=t).astype("float64")
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df["sun"] = df["sun"].shift(-1, freq="h").reindex(df.index)  # store at the start of the hour
    return df


def parse_openmeteo(payload: dict) -> pd.DataFrame:
    h = payload["hourly"]
    t = pd.to_datetime(h["time"]).tz_localize("UTC")  # we always ask for GMT
    df = pd.DataFrame({"temp": h["temperature_2m"],
                       "sun": np.asarray(h.get("sunshine_duration", [np.nan] * len(t)), dtype="float64") / 60.0},
                      index=t).astype("float64")
    df["sun"] = df["sun"].shift(-1, freq="h").reindex(df.index)
    return df


def _brightsky(cfg: Settings, lat: float, lon: float, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    r = get(f"{cfg['sources']['brightsky']['base_url']}/weather",
            params={"lat": lat, "lon": lon, "date": start.isoformat(), "last_date": end.isoformat()})
    return parse_brightsky(r.json())


def _openmeteo(cfg: Settings, lat: float, lon: float, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    days = max(1, int(np.ceil((end - pd.Timestamp.now(tz="UTC")) / pd.Timedelta(days=1))) + 1)
    r = get(cfg["sources"]["openmeteo"]["base_url"],
            params={"latitude": lat, "longitude": lon, "hourly": "temperature_2m,sunshine_duration",
                    "timezone": "GMT", "past_days": 3, "forecast_days": min(days, 10)})
    df = parse_openmeteo(r.json())
    return df[(df.index >= start) & (df.index < end)]


def fetch(cfg: Settings, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Wide table temp_<point>, sun_<point> for [start, end) UTC, plus the source used."""
    stations = dwd.get_stations(cfg)
    cols, used = {}, {}
    for point, info in stations["temp"].items():
        df = None
        for name, fn in (("brightsky", _brightsky), ("openmeteo", _openmeteo)):
            try:
                df = fn(cfg, info["lat"], info["lon"], start, end)
                if df["temp"].notna().sum() > 0:
                    used[point] = name
                    break
            except Exception as e:  # noqa: BLE001 - try the backup source
                log.warning("Forecast from %s failed for %s: %s", name, point, e)
                df = None
        if df is None:
            log.error("No forecast for %s; it will be left out of the national average", point)
            continue
        cols[f"temp_{point}"] = df["temp"]
        cols[f"sun_{point}"] = df["sun"]
    out = pd.DataFrame(cols)
    idx = pd.date_range(start, end, freq="h", inclusive="left")
    out = out.reindex(idx).interpolate(limit=3, limit_direction="both")
    out.index.name = "time_utc"
    out.attrs["sources"] = used
    return out


def fetch_and_store(cfg: Settings, start: pd.Timestamp, end: pd.Timestamp, issued: pd.Timestamp) -> pd.DataFrame:
    df = fetch(cfg, start, end)
    stored = df.copy()
    stored["issued_utc"] = issued
    store.save_monthly(cfg, NAME, stored)
    return df
