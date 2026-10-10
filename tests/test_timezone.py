"""Clock changes in March and October: the classic bug in German energy data."""
import json

import numpy as np
import pandas as pd
import pytest

from desave import features as F
from desave.models import baseline
from desave.sources import smard
from desave.timeutils import (expected_hours, hourly_utc_range, local_dates, lookup_shifted, shift_local,
                              winter_bounds)
from conftest import hourly


@pytest.mark.parametrize("day,n", [("2023-03-26", 23), ("2023-10-29", 25), ("2023-06-01", 24),
                                   ("2024-03-31", 23), ("2024-10-27", 25), ("2025-03-30", 23),
                                   ("2025-10-26", 25), ("2026-03-29", 23), ("2026-10-25", 25)])
def test_hours_per_day(day, n):
    assert expected_hours(day) == n
    assert len(hourly_utc_range(day, pd.Timestamp(day) + pd.Timedelta(days=1))) == n


def test_local_hours_on_change_days():
    idx = hourly_utc_range("2023-03-26", "2023-03-27")
    hours = list(idx.tz_convert("Europe/Berlin").hour)
    assert 2 not in hours and len(hours) == 23            # 02:00 does not exist in March
    idx = hourly_utc_range("2023-10-29", "2023-10-30")
    hours = list(idx.tz_convert("Europe/Berlin").hour)
    assert hours.count(2) == 2 and len(hours) == 25        # 02:00 happens twice in October


def test_same_week_last_year_matches_wall_clock():
    """8:00 German time must be compared with 8:00 a year earlier, even when one of
    the two dates is in summer time and the other is not (clocks change on different dates)."""
    s = hourly("2023-01-01", "2024-12-31")
    loc = s.index.tz_convert("Europe/Berlin")
    s[:] = loc.hour * 1000 + 30_000                         # value tells the local hour
    # 28 March 2024 is still winter time; 364 days earlier (30 March 2023) was summer time
    target = hourly_utc_range("2024-03-28", "2024-03-29")
    b = baseline.same_week_last_year(s, target)
    assert np.allclose(b.to_numpy(), target.tz_convert("Europe/Berlin").hour * 1000 + 30_000)
    # the naive UTC shift would be one hour off: prove the bug exists so the test means something
    naive = s.reindex(target - pd.Timedelta(days=364)).to_numpy()
    assert not np.allclose(naive, b.to_numpy())


def test_shift_over_october_double_hour():
    target = hourly_utc_range("2023-10-29", "2023-10-30")      # 25 hours
    back = shift_local(target, 7)                             # 22 October: normal 24-hour day
    assert back.tz_convert("Europe/Berlin").hour.tolist() == target.tz_convert("Europe/Berlin").hour.tolist()


def test_lookup_on_missing_spring_hour():
    s = hourly("2023-03-20", "2023-04-10")
    target = hourly_utc_range("2023-04-02", "2023-04-03")     # normal day; a week earlier lacks 02:00
    v = lookup_shifted(s, target, 7)
    assert v.notna().all()


def test_daily_totals_by_german_day():
    s = hourly("2023-10-28", "2023-10-31", 1.0)
    totals = s.groupby(local_dates(s.index)).sum()
    assert list(totals) == [24, 25, 24]


def test_features_on_change_days(cfg):
    idx = hourly_utc_range("2023-10-28", "2023-10-31")
    w = pd.DataFrame({"temp": 10.0, "sun": 0.0}, index=idx)
    X = F.build(cfg, idx, w)
    assert X["hour"].between(0, 23).all()
    assert X.loc[X.index.tz_convert("Europe/Berlin").date == pd.Timestamp("2023-10-29").date(), "dst"].sum() == 3


def test_smard_chunk_over_clock_change():
    """A SMARD weekly chunk that contains the March change has 167 hours, in UTC, without duplicates."""
    start = pd.Timestamp("2023-03-20", tz="Europe/Berlin").tz_convert("UTC")
    end = pd.Timestamp("2023-03-27", tz="Europe/Berlin").tz_convert("UTC")
    idx = pd.date_range(start, end, freq="h", inclusive="left")
    payload = {"meta_data": {"version": 1}, "series": [[int(t.value // 10**6), 40000.0 + i] for i, t in enumerate(idx)]}
    s = smard.parse_chunk(json.loads(json.dumps(payload)))
    assert len(s) == 167 and s.index.is_unique and str(s.index.tz) == "UTC"


def test_winter_bounds_are_local_midnight():
    s, e = winter_bounds("2022/23")
    assert s.tz_convert("Europe/Berlin") == pd.Timestamp("2022-10-01", tz="Europe/Berlin")
    assert e.tz_convert("Europe/Berlin") == pd.Timestamp("2023-04-01", tz="Europe/Berlin")


@pytest.mark.realdata
def test_real_data_change_days(real_load, cfg):
    days = real_load.groupby(local_dates(real_load.index, cfg.tz)).size()
    for y in range(2016, pd.Timestamp.now().year):
        march = max(d for d in days.index if d.year == y and d.month == 3 and d.weekday() == 6)
        october = max(d for d in days.index if d.year == y and d.month == 10 and d.weekday() == 6)
        assert days[march] == 23 and days[october] == 25
