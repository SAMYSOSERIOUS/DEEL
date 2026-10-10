"""The daily job (runs early morning, German time).

  1. Download new demand for Germany and the 4 grid regions (SMARD; Energy-Charts
     fills missing German hours) and the weather (DWD).
  2. Check the data. Bad German data stops the job (the web page then shows a warning);
     a problem in one grid region is reported but doesn't stop the rest.
  3. Get the weather forecast ONCE, then forecast TOMORROW hour by hour, with a range,
     for Germany and for each grid region.
  4. Score every earlier forecast whose day is now complete against what really happened.
  5. Rebuild the web page.

Every forecast is saved exactly as issued (data/forecasts/, regions in
data/forecasts/areas/<region>/), so scores can't be quietly improved later.
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .. import areas, dataset
from .. import features as F
from ..config import Settings
from ..models import calibration, operational
from ..sources import dwd, energy_charts, entsoe, smard, weather_forecast
from ..timeutils import expected_hours, hourly_utc_range, local_dates

log = logging.getLogger(__name__)


def today_local(cfg: Settings) -> date:
    return pd.Timestamp.now(tz=cfg.tz).date()


def forecasts_dir(cfg: Settings):
    d = cfg.path("data") / "forecasts"
    if cfg.area != areas.COUNTRY:
        d = d / "areas" / cfg.area
    d.mkdir(parents=True, exist_ok=True)
    return d


def update_data(cfg: Settings) -> dict:
    status = {}
    smard.update(cfg)
    status["smard"] = "ok"
    for code in areas.region_codes(cfg):
        try:
            smard.update(areas.scoped(cfg, code))
            status[f"smard_{code}"] = "ok"
        except Exception as e:  # noqa: BLE001 - one region failing must not stop Germany
            status[f"smard_{code}"] = f"{type(e).__name__}: {e}"
            log.warning("SMARD %s failed: %s", code, e)
    # backups for missing SMARD hours: Energy-Charts (no key), then ENTSO-E (only if a key is set)
    try:
        status["energy_charts_filled_hours"] = energy_charts.fill_gaps(cfg)
    except Exception as e:  # noqa: BLE001 - a backup failing must not stop the job
        status["energy_charts"] = f"{type(e).__name__}: {e}"
        try:
            status["entsoe_filled_hours"] = entsoe.fill_gaps(cfg)
        except Exception as e2:  # noqa: BLE001 - no key or no overlap: fine, just report
            status["entsoe"] = str(e2)
    dwd.update(cfg)
    status["dwd"] = "ok"
    return status


def issue_forecast(cfg: Settings, issue_day: date, model=None, load: pd.Series | None = None,
                   weather: pd.DataFrame | None = None, backfilled: bool = False,
                   forecast_wide: pd.DataFrame | None = None) -> pd.DataFrame:
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
        fc = forecast_wide if forecast_wide is not None else fetch_weather_forecast(cfg, issue_day)
        src = ",".join(sorted(set(fc.attrs.get("sources", {}).values()))) or "unknown"
        weather = dataset.weather_with_forecast(cfg, fc, start=hours.min() - pd.Timedelta(days=30))
    df = operational.forecast(cfg, model, hours, weather, load_known)
    # widen the range by the factor that made recent ranges really cover ~90% (1.0 for backfilled days)
    df = calibration.apply(df, 1.0 if backfilled else calibration.load_scale(cfg))
    df["target_date"] = str(target)
    df["issued_utc"] = issued
    df["model_retrained"] = model.meta.get("retrained_utc", "")
    df["weather_source"] = src
    df["backfilled"] = backfilled
    df.index.name = "time_utc"
    df.to_parquet(forecasts_dir(cfg) / f"{target}.parquet")
    return df


def fetch_weather_forecast(cfg: Settings, issue_day: date) -> pd.DataFrame:
    """The weather forecast for all stations, fetched once and shared by every area."""
    target = issue_day + timedelta(days=1)
    hours = hourly_utc_range(target, target + timedelta(days=1), cfg.tz)
    issued = (pd.Timestamp(issue_day).tz_localize(cfg.tz)
              + pd.Timedelta(hours=cfg["production"]["issue_hour_local"])).tz_convert("UTC")
    return weather_forecast.fetch_and_store(cfg, issued.floor("h") - pd.Timedelta(days=2),
                                            hours.max() + pd.Timedelta(hours=1), issued)


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


HEAL_BUDGET_MIN = 75   # stop starting repairs after this many minutes; the rest waits for tomorrow


def heal(cfg: Settings, started: pd.Timestamp | None = None, budget_min: float = HEAL_BUDGET_MIN) -> list[str]:
    """Fill what a grid region is missing, so it never stays empty on the page.

    The full analysis normally comes from the first-run and monthly jobs. If those
    haven't run since the region was added (or failed), the daily job does the
    missing parts itself: 30 days of backfilled forecasts and the savings analysis.
    """
    done = []
    started = started or pd.Timestamp.now(tz="UTC")
    def time_left() -> bool:
        return (pd.Timestamp.now(tz="UTC") - started).total_seconds() / 60 < budget_min
    if all_forecasts(cfg).empty and time_left():
        backfill(cfg, days=30)
        done.append("backfill")
    if not (cfg.path("results") / "savings.json").exists() and time_left():
        from ..analysis import savings
        savings.run(cfg, savings.prepare(cfg), light=True)
        done.append("savings")
    return done


def forecast_area(cfg: Settings, day: date, forecast_wide: pd.DataFrame | None, load: pd.Series | None = None,
                  started: pd.Timestamp | None = None) -> dict:
    """Calibrate the range, forecast tomorrow and score past days for one measured area."""
    load = dataset.load_series(cfg) if load is None else load
    if operational.load_current(cfg) is None:
        operational.train(cfg, load, dataset.weather_observed(cfg), force=True)
    healed = heal(cfg, started) if cfg.area != areas.COUNTRY else []
    cal = calibration.compute(all_forecasts(cfg), load)
    calibration.save(cfg, cal)
    fc = issue_forecast(cfg, day, load=load, forecast_wide=forecast_wide)
    sc = score(cfg, load)
    return {"range_scale": cal["scale"], "forecast_for": fc["target_date"].iloc[0],
            "forecast_total_gwh": round(float(fc["forecast_mwh"].sum() / 1000), 1), "days_scored": int(len(sc)),
            **({"repaired": healed} if healed else {})}


def run(cfg: Settings, skip_download: bool = False, issue_day: date | None = None) -> dict:
    status = {"started_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")}
    try:
        if not skip_download:
            status["download"] = update_data(cfg)
        load = dataset.load_series(cfg)          # raises if the German data checks fail
        status["data_check"] = "passed"
    except Exception as e:  # noqa: BLE001
        status["error"] = f"{type(e).__name__}: {e}"
        _write_status(cfg, status)
        from ..web import build as site
        site.build(cfg)
        raise
    day = issue_day or today_local(cfg)
    wide = fetch_weather_forecast(cfg, day)
    status.update(forecast_area(cfg, day, wide, load))
    status["areas"] = {}
    for code in areas.region_codes(cfg):
        try:
            status["areas"][code] = forecast_area(areas.scoped(cfg, code), day, wide,
                                                  started=pd.Timestamp(status["started_utc"]))
        except Exception as e:  # noqa: BLE001 - report it, keep Germany and the other regions
            status["areas"][code] = {"error": f"{type(e).__name__}: {e}"}
            log.warning("Daily forecast for %s failed: %s", code, e)
    status["finished_utc"] = pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds")
    _write_status(cfg, status)
    from ..web import build as site
    site.build(cfg)
    return status


def _write_status(cfg: Settings, status: dict) -> None:
    (cfg.path("results") / "last_run.json").write_text(json.dumps(status, indent=2, default=str))

