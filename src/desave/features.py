"""Everything the model gets to see ("features").

Calendar:  German local hour, weekday, day of year, summer time.
Holidays:  share of Germans with a public holiday today (state holidays are
           weighted by state population), bridge days, school holidays,
           Christmas / New Year period, and the same for yesterday/tomorrow.
Weather:   population-weighted temperature now, over the last day, and a slow
           "buildings cool down" average; heating and cooling degrees;
           sunshine; sun angle (for rooftop solar and lighting).
Covid:     lockdown strength (set to 0 when forecasting "normal").
Level:     which "September-to-August" year it is (2014/15 = 0, 2015/16 = 1, ...).
           Lets the model use the most recent full year's demand level instead
           of the 2015-2022 average. Trees can't extend a trend, so after the
           training data ends this simply holds the last full year's level.
           Whole years (not a smooth clock) stop the model from memorising the
           last few summer weeks and carrying them into winter.
Lags:      ONLY for the daily operational model: demand a week ago, two weeks
           ago, and the average of recent days (at least 3 days old).

The "what would have happened" model never sees past demand. Otherwise it
would learn the crisis savings from the recent past and hide them.
"""
from __future__ import annotations

from datetime import date
from functools import lru_cache

import holidays as hol
import numpy as np
import pandas as pd

from .config import Settings
from .timeutils import TZ, local_dates, local_index, lookup_shifted, sun_elevation_deg

CALENDAR = ["hour", "dow", "is_weekend", "doy", "month", "dst",
            "holiday_share", "holiday_share_prev", "holiday_share_next", "national_holiday",
            "bridge_share", "school_share", "is_dec24", "is_dec31", "days_to_xmas", "between_years"]
WEATHER = ["temp", "temp_24h", "temp_72h", "temp_daily", "hdd", "cdd", "sun", "sun_daily", "sun_elev"]
OTHER = ["covid", "level_year"]
LAGS = ["lag_7d", "lag_14d", "recent_level"]

CF_FEATURES = CALENDAR + WEATHER + OTHER          # counterfactual ("no crisis") model
OP_FEATURES = CF_FEATURES + LAGS                   # daily operational model


# ------------------------------------------------------------------ holidays
def state_weights(cfg: Settings) -> pd.Series:
    w = pd.Series(dtype="float64")
    for p in cfg["weather_points"]:
        w[p["state"]] = w.get(p["state"], 0.0) + float(p["pop_k"])
    return w / w.sum()


@lru_cache(maxsize=64)
def _state_holidays(state: str, years: tuple[int, ...], partial_items: tuple, school: bool):
    public = hol.Germany(subdiv=state, years=list(years))
    partial = dict(partial_items)
    out = {d: 1.0 for d in public}
    if partial:
        extra = hol.Germany(subdiv=state, years=list(years), categories=("catholic",))
        for d, name in extra.items():
            for key, share in partial.items():
                if key in name:
                    out[d] = max(out.get(d, 0.0), share)
    sch = {}
    if school:
        try:
            sch = {d: 1.0 for d in hol.Germany(subdiv=state, years=list(years), categories=("school",))}
        except Exception:  # noqa: BLE001 - older package versions have no school holidays
            sch = {}
    return out, sch


def holiday_table(cfg: Settings, start: date, end: date) -> pd.DataFrame:
    """One row per German calendar day from start to end (inclusive)."""
    days = pd.date_range(pd.Timestamp(start) - pd.Timedelta(days=2), pd.Timestamp(end) + pd.Timedelta(days=2), freq="D")
    years = tuple(range(days.min().year, days.max().year + 1))
    w = state_weights(cfg)
    partial_cfg = cfg["holidays"].get("partial", {}) or {}
    school_on = bool(cfg["holidays"].get("use_school_holidays", True))

    hshare = np.zeros(len(days))
    bshare = np.zeros(len(days))
    sshare = np.zeros(len(days))
    national = np.ones(len(days), dtype=bool)
    dts = [d.date() for d in days]
    weekday = np.array([d.weekday() for d in dts])
    for state, weight in w.items():
        h, s = _state_holidays(state, years, tuple(sorted((partial_cfg.get(state) or {}).items())), school_on)
        hv = np.array([h.get(d, 0.0) for d in dts])
        hshare += weight * hv
        sshare += weight * np.array([s.get(d, 0.0) for d in dts])
        national &= hv >= 1.0
        is_off = (hv >= 0.5) | (weekday >= 5)
        prev_h = np.concatenate([[False], hv[:-1] >= 0.5])
        next_h = np.concatenate([hv[1:] >= 0.5, [False]])
        prev_off = np.concatenate([[False], is_off[:-1]])
        next_off = np.concatenate([is_off[1:], [False]])
        # a working day squeezed between a holiday and a weekend (or two days off)
        bridge = (~is_off) & ((prev_h & next_off) | (next_h & prev_off))
        bshare += weight * bridge
    t = pd.DataFrame({
        "holiday_share": hshare, "bridge_share": bshare, "school_share": sshare,
        "national_holiday": national.astype(int),
    }, index=pd.Index(dts, name="local_date"))
    t["holiday_share_prev"] = t["holiday_share"].shift(1)
    t["holiday_share_next"] = t["holiday_share"].shift(-1)
    m = np.array([d.month for d in dts])
    dd = np.array([d.day for d in dts])
    t["is_dec24"] = ((m == 12) & (dd == 24)).astype(int)
    t["is_dec31"] = ((m == 12) & (dd == 31)).astype(int)
    xmas = np.array([(d - date(d.year if d.month == 12 or d.day > 10 else d.year - 1, 12, 25)).days
                     if (d.month == 12 or (d.month == 1 and d.day <= 10)) else 99 for d in dts])
    t["days_to_xmas"] = np.where(np.abs(xmas) <= 20, xmas, 99)
    t["between_years"] = (((m == 12) & (dd >= 27)) | ((m == 1) & (dd <= 5))).astype(int) * (weekday < 5)
    return t.loc[start:end]


def is_german_holiday(cfg: Settings, d: date, state: str | None = None) -> bool:
    if state:
        return d in hol.Germany(subdiv=state, years=d.year)
    return bool(holiday_table(cfg, d, d)["holiday_share"].iloc[0] > 0)


# -------------------------------------------------------------------- covid
def covid_intensity(cfg: Settings, idx: pd.DatetimeIndex) -> np.ndarray:
    out = np.zeros(len(idx))
    ld = pd.DatetimeIndex(pd.to_datetime(local_dates(idx, cfg.tz)))
    for p in cfg["covid"]["periods"]:
        m = (ld >= pd.Timestamp(p["start"])) & (ld <= pd.Timestamp(p["end"]))
        out[m] = np.maximum(out[m], p["intensity"])
    return out


# ------------------------------------------------------------------ weather
def weather_features(weather: pd.DataFrame, idx: pd.DatetimeIndex, tz: str = TZ) -> pd.DataFrame:
    """``weather`` has national hourly 'temp' and 'sun' (UTC). Rolling values use the full input."""
    full = pd.date_range(min(weather.index.min(), idx.min()), max(weather.index.max(), idx.max()), freq="h")
    w = weather.reindex(full)
    temp = w["temp"].interpolate(limit=6, limit_direction="both")
    sun = w["sun"].interpolate(limit=3, limit_direction="both") if "sun" in w else pd.Series(np.nan, index=full)
    out = pd.DataFrame(index=full)
    out["temp"] = temp
    out["temp_24h"] = temp.rolling(24, min_periods=12).mean()
    out["temp_72h"] = temp.ewm(span=72, min_periods=12).mean()
    ld = local_dates(full, tz)
    out["temp_daily"] = temp.groupby(ld).transform("mean").to_numpy()
    out["hdd"] = np.clip(15.0 - out["temp_daily"], 0, None)
    out["cdd"] = np.clip(out["temp_24h"] - 20.0, 0, None)
    out["sun"] = sun
    out["sun_daily"] = sun.groupby(ld).transform("sum").to_numpy()
    out["sun_elev"] = sun_elevation_deg(full)
    return out.reindex(idx)


# --------------------------------------------------------------------- lags
def lag_features(load: pd.Series, idx: pd.DatetimeIndex, min_lag_days: int = 3, tz: str = TZ) -> pd.DataFrame:
    """Past demand, aligned on German wall-clock time (so clock changes don't shift it by an hour)."""
    out = pd.DataFrame(index=idx)
    out["lag_7d"] = lookup_shifted(load, idx, 7, tz).to_numpy()
    out["lag_14d"] = lookup_shifted(load, idx, 14, tz).to_numpy()
    ld = local_dates(load.index, tz)
    g = load.groupby(ld)
    daily = g.mean().where(g.count() >= 20)          # ignore days that are only partly published
    daily.index = pd.to_datetime(daily.index)
    target_days = pd.to_datetime(local_dates(idx, tz))
    # extend the calendar to the days we forecast, so "3 days earlier" exists for them too
    full = pd.date_range(min(daily.index.min(), target_days.min()), max(daily.index.max(), target_days.max()), freq="D")
    daily = daily.reindex(full)
    level = daily.rolling(7, min_periods=5).mean().shift(min_lag_days)
    out["recent_level"] = level.reindex(target_days).to_numpy()
    return out


# -------------------------------------------------------------------- build
def build(cfg: Settings, idx: pd.DatetimeIndex, weather: pd.DataFrame, load: pd.Series | None = None,
          normal_world: bool = False) -> pd.DataFrame:
    """All features for the UTC hours in ``idx``.

    ``normal_world=True`` switches off the Covid marker (used for "no crisis" forecasts).
    """
    tz = cfg.tz
    loc = local_index(idx, tz)
    X = pd.DataFrame(index=idx)
    X["hour"] = loc.hour
    X["dow"] = loc.dayofweek
    X["is_weekend"] = (loc.dayofweek >= 5).astype(int)
    X["doy"] = loc.dayofyear
    X["month"] = loc.month
    X["dst"] = (loc.tz_localize(None) - idx.tz_localize(None) == pd.Timedelta(hours=2)).astype(int)
    ld = local_dates(idx, tz)
    ht = holiday_table(cfg, min(ld), max(ld))
    X = X.join(ht.reindex(ld).set_index(idx)[[c for c in CALENDAR if c in ht.columns]])
    X = X.join(weather_features(weather, idx, tz))
    X["covid"] = 0.0 if normal_world else covid_intensity(cfg, idx)
    X["level_year"] = np.where(loc.month >= 9, loc.year, loc.year - 1) - 2014
    if load is not None:
        X = X.join(lag_features(load, idx, cfg["production"]["min_lag_days"], tz))
    return X
