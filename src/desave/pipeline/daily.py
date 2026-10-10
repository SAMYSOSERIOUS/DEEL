"""The daily job (runs early morning, German time).

  1. Download new demand (SMARD; ENTSO-E fills gaps if a key is set) and weather (DWD).
  2. Check the data. Bad data stops the job (the web page then shows a warning).
  3. Get the weather forecast and forecast TOMORROW hour by hour, with a range.
  4. Score every earlier forecast whose day is now complete against what really happened.
  5. Rebuild the web page.

Every forecast is saved exactly as issued (data/forecasts/), so scores can't
be quietly improved later.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import dataset
from .. import features as F
from ..config import Settings
from ..models import operational
from ..sources import dwd, entsoe, smard, weather_forecast
from ..timeutils import expected_hours, hourly_utc_range, local_dates

log = logging.getLogger(__name__)


def today_local(cfg: Settings) -> date:
    return pd.Timestamp.now(tz=cfg.tz).date()


def forecasts_dir(cfg: Settings):
    d = cfg.path("data") / "forecasts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_data(cfg: Settings) -> dict:
    status = {}
    smard.update(cfg)
    status["smard"] = "ok"
    try:
        n = entsoe.fill_gaps(cfg)
        status["entsoe_filled_hours"] = n
    except RuntimeError as e:  # no key or no overlap: fine, just report
        status["entsoe"] = str(e)
    dwd.update(cfg)
    status["dwd"] = "ok"
    return status


def issue_forecast(cfg: Settings, issue_day: date, model=None, load: pd.Series | None = None,
                   weather: pd.DataFrame | None = None, backfilled: bool = False) -> pd.DataFrame:
    """Forecast the local day after ``issue_day`` and save it."""
    target = issue_day + timedelta(days=1)
    hours = hourly_utc_range(target, target + timedelta(days=1), cfg.tz)
    model = model or operational.load_current(cfg)
    if model is None:
        raise RuntimeError("No trained model yet. Run: desave retrain")
    load = dataset.load_series(cfg) if load is None else load
    issued = pd.Timestamp(issue_day).tz_localize(cfg.tz) + pd.Timedelta(hours=cfg["production"]["issue_hour_local"])
    issued = issued.tz_convert("UTC")
    load_known = load[load.index < issued]  # never use data from after the forecast was made
    src = "measured (backfill)" if backfilled else None
    if weather is None:
        fc = weather_forecast.fetch_and_store(cfg, issued.floor("h") - pd.Timedelta(days=2),
                                              hours.max() + pd.Timedelta(hours=1), issued)
        src = ",".join(sorted(set(fc.attrs.get("sources", {}).values()))) or "unknown"
        weather = dataset.weather_with_forecast(cfg, fc, start=hours.min() - pd.Timedelta(days=30))
    df = operational.forecast(cfg, model, hours, weather, load_known)
    df["target_date"] = str(target)
    df["issued_utc"] = issued
    df["model_retrained"] = model.meta.get("retrained_utc", "")
    df["weather_source"] = src
    df["backfilled"] = backfilled
    df.index.name = "time_utc"
    df.to_parquet(forecasts_dir(cfg) / f"{target}.parquet")
    return df


def all_forecasts(cfg: Settings) -> pd.DataFrame:
    files = sorted(forecasts_dir(cfg).glob("*.parquet"))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files]).sort_index()


def score(cfg: Settings, load: pd.Series | None = None) -> pd.DataFrame:
    """Score each forecast day that is now complete. One row per day."""
    fc = all_forecasts(cfg)
    if fc.empty:
        return pd.DataFrame()
    load = dataset.load_series(cfg, check=False) if load is None else load
    rows = []
    for day, g in fc.groupby("target_date"):
        a = load.reindex(g.index)
        if a.notna().sum() < expected_hours(day, cfg.tz):
            continue  # not all hours published yet
        err = (g["forecast_mwh"] - a) / a
        berr = (g["baseline_mwh"] - a) / a
        inside = ((a >= g["q05_mwh"]) & (a <= g["q95_mwh"])) if "q05_mwh" in g else pd.Series(np.nan, index=a.index)
        rows.append({
            "date": day, "hours": int(a.notna().sum()),
            "model_mape": float(100 * err.abs().mean()), "baseline_mape": float(100 * berr.abs().mean()),
            "model_daily_error_pct": float(100 * (g["forecast_mwh"].sum() / a.sum() - 1)),
            "inside_range_pct": float(100 * inside.mean()),
            "actual_gwh": float(a.sum() / 1000), "forecast_gwh": float(g["forecast_mwh"].sum() / 1000),
            "backfilled": bool(g["backfilled"].iloc[0]) if "backfilled" in g else False,
        })
    df = pd.DataFrame(rows).sort_values("date") if rows else pd.DataFrame()
    if len(df):
        df.to_csv(cfg.path("results") / "scores.csv", index=False)
    return df


def backfill(cfg: Settings, days: int = 30) -> int:
    """Fill the last N days with forecasts as they WOULD have been made.

    Honest version: uses a model trained only on data before the window, and
    measured weather instead of the (unavailable) old weather forecasts, which
    makes these days look a little better than real forecasts. They are
    marked as "backfilled" on the page.
    """
    load = dataset.load_series(cfg)
    weather = dataset.weather_observed(cfg)
    last_day = local_dates(load.index[-1:], cfg.tz)[0]
    first_issue = last_day - timedelta(days=days)
    cut = pd.Timestamp(first_issue).tz_localize(cfg.tz).tz_convert("UTC")
    X = F.build(cfg, load.index[load.index < cut], weather, load=load)
    model = operational.new_model(cfg)
    model.fit(X, load.reindex(X.index))
    model.meta["retrained_utc"] = f"backfill model (data before {first_issue})"
    n = 0
    for k in range(days):
        issue = first_issue + timedelta(days=k)
        if (forecasts_dir(cfg) / f"{issue + timedelta(days=1)}.parquet").exists():
            continue
        issue_forecast(cfg, issue, model=model, load=load, weather=weather, backfilled=True)
        n += 1
    return n


def run(cfg: Settings, skip_download: bool = False, issue_day: date | None = None) -> dict:
    status = {"started_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")}
    try:
        if not skip_download:
            status["download"] = update_data(cfg)
        load = dataset.load_series(cfg)          # raises if the data checks fail
        status["data_check"] = "passed"
    except Exception as e:  # noqa: BLE001
        status["error"] = f"{type(e).__name__}: {e}"
        _write_status(cfg, status)
        from ..web import build as site
        site.build(cfg)
        raise
    if operational.load_current(cfg) is None:
        from . import retrain
        retrain.run(cfg, include_analysis=False)
    day = issue_day or today_local(cfg)
    fc = issue_forecast(cfg, day, load=load)
    status["forecast_for"] = fc["target_date"].iloc[0]
    status["forecast_total_gwh"] = round(float(fc["forecast_mwh"].sum() / 1000), 1)
    sc = score(cfg, load)
    status["days_scored"] = int(len(sc))
    status["finished_utc"] = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    _write_status(cfg, status)
    from ..web import build as site
    site.build(cfg)
    return status


def _write_status(cfg: Settings, status: dict) -> None:
    (cfg.path("results") / "last_run.json").write_text(json.dumps(status, indent=2, default=str))

