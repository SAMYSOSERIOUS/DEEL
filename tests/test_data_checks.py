"""Data tests: 24 hours per day (23/25 on clock-change days), no negative demand,
no gap longer than 2 hours."""
import numpy as np
import pandas as pd
import pytest

from desave import validate
from conftest import hourly


def test_clean_year_passes():
    s = hourly("2023-01-01", "2024-01-01")
    rep = validate.check_load(s)
    assert rep.ok, rep.problems
    assert rep.stats["days_23h"] == 1 and rep.stats["days_25h"] == 1


def test_negative_demand_fails():
    s = hourly("2023-01-01", "2023-02-01")
    s.iloc[100] = -5
    rep = validate.check_load(s)
    assert not rep.ok and any("negative" in p for p in rep.problems)


def test_zero_demand_fails():
    s = hourly("2023-01-01", "2023-02-01")
    s.iloc[10] = 0
    assert not validate.check_load(s).ok


def test_two_hour_gap_is_allowed_and_filled():
    s = hourly("2023-01-01", "2023-02-01")
    s = s.drop(s.index[200:202])
    rep = validate.check_load(s)
    assert rep.ok, rep.problems
    filled = validate.fill_short_gaps(s)
    assert filled.isna().sum() == 0 and len(filled) == 31 * 24


def test_three_hour_gap_fails_and_is_not_filled():
    s = hourly("2023-01-01", "2023-02-01")
    s = s.drop(s.index[200:203])
    rep = validate.check_load(s)
    assert not rep.ok and any("gaps longer" in p for p in rep.problems)
    filled = validate.fill_short_gaps(s)
    assert filled.isna().sum() == 3  # a long gap must stay visible, never be invented


def test_duplicate_hours_fail():
    s = hourly("2023-01-01", "2023-01-10")
    s = pd.concat([s, s.iloc[[5]]]).sort_index()
    assert not validate.check_load(s).ok


def test_day_with_extra_hour_fails():
    s = hourly("2023-01-01", "2023-01-10")
    extra = pd.Series([50_000.0], index=[s.index[30] + pd.Timedelta(minutes=30)])
    s = pd.concat([s, extra]).sort_index()
    assert not validate.check_load(s).ok


def test_naive_times_fail():
    s = hourly("2023-01-01", "2023-01-10")
    s.index = s.index.tz_localize(None)
    assert not validate.check_load(s).ok


def test_implausible_values_warn():
    s = hourly("2023-01-01", "2023-01-10")
    s.iloc[3] = 150_000
    rep = validate.check_load(s)
    assert rep.ok and rep.warnings


def test_weather_checks():
    t = pd.Series(np.linspace(-5, 30, 500), index=pd.date_range("2023-01-01", periods=500, freq="h", tz="UTC"))
    assert validate.check_weather(t).ok
    bad = t.copy()
    bad.iloc[5] = 80
    assert not validate.check_weather(bad).ok
    gap = t.drop(t.index[100:110])
    assert not validate.check_weather(gap).ok


@pytest.mark.realdata
def test_real_demand_passes_checks(real_load, cfg):
    rep = validate.check_load(real_load, cfg.tz)
    assert rep.ok, rep.problems
