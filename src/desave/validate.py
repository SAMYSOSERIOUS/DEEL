"""Data checks. The same functions are used by the tests and by the daily job,
so bad data stops the pipeline instead of quietly producing a wrong forecast.

Checks on electricity demand:
  * every German calendar day has 24 hours (23 on the March clock change, 25 in October)
  * no negative (or zero) demand, and nothing wildly outside the normal range
  * no gap longer than 2 hours (short gaps are filled by drawing a straight line)
  * no repeated time stamps
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .timeutils import TZ, expected_hours, local_dates

MAX_GAP_HOURS = 2
PLAUSIBLE_MWH = (20_000, 100_000)   # German hourly grid load is roughly 35-80 GWh


@dataclass
class Report:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.problems.append(msg)

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise ValueError("Data check failed:\n  - " + "\n  - ".join(self.problems))


def missing_runs(values: pd.Series, freq: str = "h") -> pd.Series:
    """Length (in hours) of each run of missing values, indexed by the first missing hour."""
    full = pd.date_range(values.index.min(), values.index.max(), freq=freq)
    v = values.reindex(full)
    miss = v.isna().to_numpy()
    if not miss.any():
        return pd.Series(dtype="int64")
    edges = np.diff(np.concatenate([[0], miss.astype(int), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    return pd.Series(ends - starts, index=full[starts])


def hours_per_day(index: pd.DatetimeIndex, tz: str = TZ) -> pd.DataFrame:
    days = pd.Series(1, index=index).groupby(local_dates(index, tz)).sum()
    exp = pd.Series([expected_hours(d, tz) for d in days.index], index=days.index)
    return pd.DataFrame({"hours": days, "expected": exp})


def check_load(load: pd.Series, tz: str = TZ, complete_days_only: bool = True,
               max_gap: int = MAX_GAP_HOURS) -> Report:
    rep = Report()
    if load.empty:
        rep.fail("No demand data")
        return rep
    if load.index.tz is None:
        rep.fail("Time stamps have no time zone (must be UTC)")
        return rep
    if load.index.has_duplicates:
        rep.fail(f"{int(load.index.duplicated().sum())} repeated time stamps")
        load = load[~load.index.duplicated(keep="last")]   # keep checking the rest
    if not load.index.is_monotonic_increasing:
        rep.fail("Time stamps are not in order")
        load = load.sort_index()
    vals = load.dropna()
    neg = vals[vals <= 0]
    if len(neg):
        rep.fail(f"{len(neg)} hours with zero or negative demand, first at {neg.index[0]}")
    lo, hi = PLAUSIBLE_MWH
    odd = vals[(vals < lo) | (vals > hi)]
    if len(odd):
        rep.warnings.append(f"{len(odd)} hours outside {lo:,}-{hi:,} MWh, first at {odd.index[0]}")

    runs = missing_runs(load)
    long = runs[runs > max_gap]
    if len(long):
        rep.fail(f"{len(long)} gaps longer than {max_gap} h; longest {int(long.max())} h starting {long.idxmax()}")
    rep.stats["gaps_total_hours"] = int(runs.sum()) if len(runs) else 0

    present = vals.index
    hpd = hours_per_day(present, tz)
    if complete_days_only and len(hpd) > 2:
        hpd = hpd.iloc[1:-1]  # first and last day may be cut off by the download window
    bad = hpd[hpd["hours"] != hpd["expected"]]
    # days with a short gap are allowed to miss up to max_gap hours (they get filled)
    really_bad = bad[(bad["hours"] > bad["expected"]) | (bad["expected"] - bad["hours"] > max_gap)]
    if len(really_bad):
        ex = really_bad.head(3)
        rep.fail(f"{len(really_bad)} days with the wrong number of hours, e.g. "
                 + ", ".join(f"{d}: {r.hours} (expected {r.expected})" for d, r in ex.iterrows()))
    rep.stats.update({
        "first": str(load.index.min()), "last": str(load.index.max()), "hours": int(len(vals)),
        "days_23h": int((hpd["expected"] == 23).sum()), "days_25h": int((hpd["expected"] == 25).sum()),
    })
    return rep


def fill_short_gaps(load: pd.Series, max_gap: int = MAX_GAP_HOURS) -> pd.Series:
    """Fill gaps of at most ``max_gap`` hours with a straight line; longer gaps stay empty."""
    full = pd.date_range(load.index.min(), load.index.max(), freq="h")
    s = load.reindex(full)
    filled = s.interpolate(method="time", limit=max_gap, limit_area="inside")
    # interpolate(limit=) would also part-fill longer gaps; undo that
    runs = missing_runs(s)
    for start, length in runs[runs > max_gap].items():
        filled.loc[start: start + pd.Timedelta(hours=int(length) - 1)] = np.nan
    return filled


def check_weather(temp: pd.Series, sun: pd.Series | None = None) -> Report:
    rep = Report()
    t = temp.dropna()
    if t.empty:
        rep.fail("No temperature data")
        return rep
    if (t < -35).any() or (t > 45).any():
        rep.fail("Temperature outside -35..45 °C")
    runs = missing_runs(temp)
    if len(runs) and runs.max() > 6:
        rep.fail(f"Temperature gap of {int(runs.max())} h starting {runs.idxmax()}")
    if sun is not None:
        s = sun.dropna()
        if (s < 0).any() or (s > 60.5).any():
            rep.fail("Sunshine minutes outside 0..60 per hour")
    rep.stats["temp_mean"] = round(float(t.mean()), 2)
    return rep
