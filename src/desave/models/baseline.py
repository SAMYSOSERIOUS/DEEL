"""Simple baselines. A fancy model is only worth it if it beats these.

* same_week_last_year: demand at the same German wall-clock time 52 weeks
  (364 days) earlier, so a Monday is compared with a Monday.
* pre_crisis_average: the average of the same week in normal years
  (default 2017-2019). Used for the savings question, because after 2022
  "last year" is itself a crisis year.
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd

from ..timeutils import TZ, local_index, lookup_shifted

WEEKS_52 = 364


def same_week_last_year(load: pd.Series, idx: pd.DatetimeIndex, tz: str = TZ) -> pd.Series:
    out = lookup_shifted(load, idx, WEEKS_52, tz)
    # if last year's hour is missing (e.g. the 25th hour), fall back to two years ago
    gap = out.isna()
    if gap.any():
        out[gap] = lookup_shifted(load, idx[gap], 2 * WEEKS_52, tz).to_numpy()
    return out.rename("baseline_last_year")


def pre_crisis_average(load: pd.Series, idx: pd.DatetimeIndex, reference_years: list[int],
                       tz: str = TZ, max_years_back: int = 15) -> pd.Series:
    vals = []
    for k in range(1, max_years_back + 1):
        src_local = local_index(idx, tz) - pd.Timedelta(days=WEEKS_52 * k)
        keep = np.isin(src_local.year, reference_years)
        if not keep.any():
            continue
        v = lookup_shifted(load, idx, WEEKS_52 * k, tz).to_numpy()
        vals.append(np.where(keep, v, np.nan))
    if not vals:
        return pd.Series(np.nan, index=idx, name="baseline_pre_crisis")
    arr = np.vstack(vals)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # hours with no reference year stay empty
        mean = np.nanmean(arr, axis=0)
    return pd.Series(mean, index=idx, name="baseline_pre_crisis")
