"""The area hierarchy: Germany -> 4 grid regions (measured) -> 16 states and cities (estimated)."""
from datetime import date

import pytest

from desave import areas
from desave import features as F


def test_every_state_in_exactly_one_region(cfg):
    seen = [s for r in areas.regions(cfg) for s in r["states"]]
    assert sorted(seen) == sorted(cfg["state_names"].keys())
    assert len(seen) == len(set(seen)) == 16


def test_scoped_region_settings(cfg):
    t = areas.scoped(cfg, "TenneT")
    assert t.area == "TenneT" and t.load_store == "load_TenneT"
    assert t["sources"]["smard"]["region"] == "TenneT"
    assert {p["state"] for p in t["weather_points"]} == {"SH", "NI", "HB", "HE", "BY"}
    assert t.path("results").parts[-2:] == ("areas", "TenneT")
    assert cfg.area == "DE" and cfg.load_store == "load"                     # Germany keeps its old files
    assert cfg["sources"]["smard"]["region"] == "DE"                          # the original isn't changed
    assert areas.scoped(cfg, "DE") is cfg


def test_regional_model_never_bigger(cfg):
    big = areas.scoped(cfg, "Amprion")
    assert big["model"]["lightgbm"]["n_estimators"] <= cfg["model"]["lightgbm"]["n_estimators"]
    small = cfg.with_overrides({"model": {"lightgbm": {"n_estimators": 100}, "random_seeds": [1]}})
    assert areas.scoped(small, "Amprion")["model"]["lightgbm"]["n_estimators"] == 100


def test_unknown_area(cfg):
    with pytest.raises(KeyError):
        areas.scoped(cfg, "Narnia")


def test_regional_holidays_use_only_that_regions_states(cfg):
    bw = areas.scoped(cfg, "TransnetBW")
    east = areas.scoped(cfg, "50Hertz")
    epiphany = date(2023, 1, 6)                 # holiday in BW, BY, ST
    assert F.holiday_table(bw, epiphany, epiphany)["holiday_share"].iloc[0] == pytest.approx(1.0)
    reformation = date(2023, 10, 31)            # holiday in the north and east, not in BW
    assert F.holiday_table(bw, reformation, reformation)["holiday_share"].iloc[0] == 0.0
    assert F.holiday_table(east, reformation, reformation)["holiday_share"].iloc[0] > 0.5


def test_tree_shares(cfg):
    t = areas.tree(cfg)
    assert len(t["regions"]) == 4 and len(t["states"]) == 16 and len(t["cities"]) == 19
    for r in t["regions"]:
        shares = [s["share_of_region"] for s in t["states"] if s["region"] == r["code"]]
        assert sum(shares) == pytest.approx(1.0, abs=0.01)
    for c in t["cities"]:
        assert 0 < c["share_of_state"] <= 1
    berlin = next(c for c in t["cities"] if c["code"] == "Berlin")
    assert berlin["share_of_state"] == pytest.approx(1.0)                      # Berlin is a city-state
    muc = next(c for c in t["cities"] if c["code"] == "Muenchen")
    assert 0.08 < muc["share_of_state"] < 0.15                                # ~1.5M of ~13.4M Bavarians
