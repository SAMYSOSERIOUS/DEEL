"""Feature test: the holiday function marks known German holidays correctly."""
from datetime import date

import pytest

from desave import features as F


def share(cfg, d):
    return float(F.holiday_table(cfg, d, d)["holiday_share"].iloc[0])


@pytest.mark.parametrize("d", [date(2022, 10, 3), date(2023, 10, 3), date(2024, 12, 25), date(2025, 12, 26),
                               date(2023, 1, 1), date(2023, 5, 1), date(2023, 4, 7), date(2023, 4, 10),
                               date(2023, 5, 18), date(2023, 5, 29)])
def test_nationwide_holidays(cfg, d):
    t = F.holiday_table(cfg, d, d).iloc[0]
    assert t["holiday_share"] == pytest.approx(1.0)
    assert t["national_holiday"] == 1


@pytest.mark.parametrize("d", [date(2023, 10, 4), date(2023, 12, 27), date(2023, 7, 12)])
def test_normal_days(cfg, d):
    assert share(cfg, d) == 0.0


def test_regional_holidays_are_partial(cfg):
    w = F.state_weights(cfg)
    # Epiphany (6 Jan): Baden-Württemberg, Bavaria, Saxony-Anhalt
    assert share(cfg, date(2023, 1, 6)) == pytest.approx(w["BW"] + w["BY"] + w["ST"])
    # Reformation Day (31 Oct): the northern and eastern states
    north = sum(w[s] for s in ["BB", "HB", "HH", "MV", "NI", "SN", "ST", "SH", "TH"])
    assert share(cfg, date(2023, 10, 31)) == pytest.approx(north)
    # Assumption (15 Aug): Saarland fully, Bavaria only in Catholic towns (80%)
    assert share(cfg, date(2023, 8, 15)) == pytest.approx(w["SL"] + 0.8 * w["BY"])
    # Women's Day (8 Mar): Berlin (since 2019) and Mecklenburg-Vorpommern (since 2023)
    assert share(cfg, date(2023, 3, 8)) == pytest.approx(w["BE"] + w["MV"])
    assert share(cfg, date(2018, 3, 8)) == 0.0


def test_one_time_holidays(cfg):
    # 31 Oct 2017: 500 years of the Reformation, holiday everywhere that year
    assert share(cfg, date(2017, 10, 31)) == pytest.approx(1.0)


def test_bridge_day_and_christmas(cfg):
    t = F.holiday_table(cfg, date(2023, 10, 2), date(2023, 10, 2)).iloc[0]   # Monday before Tuesday 3 Oct
    assert t["bridge_share"] == pytest.approx(1.0)
    x = F.holiday_table(cfg, date(2023, 12, 24), date(2023, 12, 28))
    assert x.loc[date(2023, 12, 24), "is_dec24"] == 1
    assert x.loc[date(2023, 12, 27), "between_years"] == 1
    assert x.loc[date(2023, 12, 25), "days_to_xmas"] == 0


def test_weights_sum_to_one(cfg):
    assert F.state_weights(cfg).sum() == pytest.approx(1.0)
    assert len(F.state_weights(cfg)) == 16
