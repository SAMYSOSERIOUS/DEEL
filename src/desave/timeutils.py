"""Time helpers.

The one rule of this project: **all data is stored in UTC**. German local time
is only used to build calendar features (hour of day, weekday, holidays) and
for charts. This avoids the classic clock-change bug in German energy data,
where the last Sunday of March has 23 hours and the last Sunday of October 25.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

TZ = "Europe/Berlin"


def utc_index_from_ms(ms) -> pd.DatetimeIndex:
    """Turn milliseconds since 1970 (what SMARD sends) into a UTC time index."""
    return pd.DatetimeIndex(pd.to_datetime(np.asarray(ms, dtype="int64"), unit="ms", utc=True))


def ensure_utc(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if idx.tz is None:
        raise ValueError("Time index has no time zone. Store data in UTC with a time zone.")
    return idx.tz_convert("UTC")


def hourly_utc_range(start_local: str | date, end_local: str | date, tz: str = TZ) -> pd.DatetimeIndex:
    """All UTC hours from local midnight of ``start_local`` up to (not incl.) local midnight of ``end_local``."""
    s = pd.Timestamp(start_local).tz_localize(tz)
    e = pd.Timestamp(end_local).tz_localize(tz)
    return pd.date_range(s.tz_convert("UTC"), e.tz_convert("UTC"), freq="h", inclusive="left")


def local_index(idx: pd.DatetimeIndex, tz: str = TZ) -> pd.DatetimeIndex:
    return ensure_utc(idx).tz_convert(tz)


def local_dates(idx: pd.DatetimeIndex, tz: str = TZ) -> pd.Index:
    """German calendar date for each UTC hour."""
    loc = local_index(idx, tz)
    return pd.Index(loc.normalize().tz_localize(None).date, name="local_date")


def expected_hours(day: date | str | pd.Timestamp, tz: str = TZ) -> int:
    """Number of hours in a German calendar day: 23 (March change), 25 (October change), else 24."""
    d = pd.Timestamp(day).normalize()
    start = d.tz_localize(tz)
    end = (d + pd.Timedelta(days=1)).tz_localize(tz)
    return int((end - start) / pd.Timedelta(hours=1))


def is_dst(idx: pd.DatetimeIndex, tz: str = TZ) -> np.ndarray:
    """True where German summer time applies."""
    utc = ensure_utc(idx)
    loc = utc.tz_convert(tz)
    offset = loc.tz_localize(None) - utc.tz_localize(None)
    return np.asarray(offset == pd.Timedelta(hours=2))


def shift_local(idx: pd.DatetimeIndex, days: int, tz: str = TZ) -> pd.DatetimeIndex:
    """Same German wall-clock time ``days`` earlier, returned in UTC.

    Shifting in UTC is WRONG around clock changes: 8:00 in winter is 7:00 UTC,
    8:00 in summer is 6:00 UTC. A year ago the clocks may have changed on a
    different date, so a UTC shift would compare 8:00 with 9:00.
    """
    utc = ensure_utc(idx)
    loc = utc.tz_convert(tz)
    naive_back = loc.tz_localize(None) - pd.Timedelta(days=days)
    back = naive_back.tz_localize(tz, ambiguous=is_dst(utc, tz), nonexistent="shift_forward")
    return back.tz_convert("UTC")


def lookup_shifted(series: pd.Series, idx: pd.DatetimeIndex, days: int, tz: str = TZ) -> pd.Series:
    """Value of ``series`` at the same German wall-clock time ``days`` earlier."""
    src = shift_local(idx, days, tz)
    vals = series.reindex(src).to_numpy()
    return pd.Series(vals, index=idx)


def winter_label(d: pd.Timestamp | date, months: list[int] | None = None) -> str | None:
    """'2022/23' for any day from October 2022 to March 2023 (or the given months)."""
    months = months or [10, 11, 12, 1, 2, 3]
    d = pd.Timestamp(d)
    if d.month not in months:
        return None
    start_year = d.year if d.month >= 7 else d.year - 1
    return f"{start_year}/{(start_year + 1) % 100:02d}"


def winter_bounds(label: str, months: list[int] | None = None, tz: str = TZ) -> tuple[pd.Timestamp, pd.Timestamp]:
    """UTC start (incl.) and end (excl.) of a winter like '2022/23'."""
    months = months or [10, 11, 12, 1, 2, 3]
    y = int(label[:4])
    first = min(m for m in months if m >= 7) if any(m >= 7 for m in months) else min(months)
    last = max(m for m in months if m < 7) if any(m < 7 for m in months) else max(months)
    start_year = y if first >= 7 else y + 1
    end_year = y + 1 if last < 7 else y
    start = pd.Timestamp(year=start_year, month=first, day=1).tz_localize(tz)
    end = (pd.Timestamp(year=end_year, month=last, day=1) + pd.offsets.MonthBegin(1)).tz_localize(tz)
    return start.tz_convert("UTC"), end.tz_convert("UTC")


def sun_elevation_deg(idx: pd.DatetimeIndex, lat: float = 51.0, lon: float = 10.0) -> np.ndarray:
    """Approximate sun angle above the horizon (degrees) at the middle of each hour.

    Accurate to about half a degree, which is plenty to tell day from night.
    Default point is the middle of Germany.
    """
    t = ensure_utc(idx) + pd.Timedelta(minutes=30)
    doy = t.dayofyear.to_numpy()
    hour = (t.hour + t.minute / 60.0).to_numpy()
    g = 2 * np.pi / 365.0 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * np.cos(g) + 0.070257 * np.sin(g) - 0.006758 * np.cos(2 * g)
            + 0.000907 * np.sin(2 * g) - 0.002697 * np.cos(3 * g) + 0.00148 * np.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * np.cos(g) - 0.032077 * np.sin(g)
                       - 0.014615 * np.cos(2 * g) - 0.040849 * np.sin(2 * g))
    solar_time = hour * 60 + eqtime + 4 * lon
    ha = np.deg2rad(solar_time / 4 - 180)
    latr = np.deg2rad(lat)
    cos_zen = np.sin(latr) * np.sin(decl) + np.cos(latr) * np.cos(decl) * np.cos(ha)
    return 90 - np.rad2deg(np.arccos(np.clip(cos_zen, -1, 1)))
