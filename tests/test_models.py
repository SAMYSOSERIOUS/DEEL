"""Model tests: the daily model beats "same week last year" on the last 4 weeks,
it never peeks at the future, and the savings maths is right."""

import numpy as np
import pandas as pd
import pytest

from desave import features as F
from desave.analysis import savings
from desave.demo import synthetic
from desave.models import operational
from desave.timeutils import hourly_utc_range, sun_elevation_deg


@pytest.fixture(scope="module")
def small_world(cfg):
    """Three years of realistic stand-in data, in memory."""
    small = cfg.with_overrides({"model": {"lightgbm": {"n_estimators": 300, "learning_rate": 0.06},
                                          "random_seeds": [1], "quantiles": [0.05, 0.95]}})
    idx = hourly_utc_range("2021-01-01", "2024-01-01", cfg.tz)
    weather = synthetic.make_weather(small, idx)
    both = synthetic.make_load(small, idx, weather, synthetic.Truth())
    w = pd.Series({p["name"]: p["pop_k"] for p in cfg["weather_points"]}, dtype=float)
    w /= w.sum()
    nat = pd.DataFrame({"temp": sum(weather[k]["temp"] * w[k] for k in w.index),
                        "sun": sum(weather[k]["sun"] * w[k] for k in w.index)})
    return small, both["load_mwh"], nat


@pytest.mark.slow
def test_model_beats_baseline_last_4_weeks(small_world):
    cfg, load, weather = small_world
    X = F.build(cfg, load.index, weather, load=load)
    res = operational.gate_check(cfg, X, load)
    assert res["model_mape"] < res["baseline_mape"], res


@pytest.mark.realdata
@pytest.mark.slow
def test_model_beats_baseline_on_real_data(cfg, real_load):
    from desave import dataset
    load = dataset.load_series(cfg)
    weather = dataset.weather_observed(cfg)
    X = F.build(cfg, load.index, weather, load=load)
    res = operational.gate_check(cfg, X, load)
    assert res["model_mape"] < res["baseline_mape"], res


def test_lag_features_never_use_recent_days(small_world):
    """Forecasting day D (made on the morning of D-1) must not use demand from D-2 or D-1:
    those days are not (fully) published yet. Day D-3 and older is allowed."""
    cfg, load, weather = small_world
    target = hourly_utc_range("2023-06-15", "2023-06-16", cfg.tz)
    before = F.lag_features(load, target, 3, cfg.tz)
    spoiled = load.copy()
    recent = (spoiled.index >= target[0] - pd.Timedelta(days=2)) & (spoiled.index < target[-1] + pd.Timedelta(hours=1))
    spoiled[recent] = 1e9                                      # make the forbidden days absurd
    after = F.lag_features(spoiled, target, 3, cfg.tz)
    pd.testing.assert_frame_equal(before, after)


def test_recent_level_present_for_forecast_day(small_world):
    """Regression test: the level of recent days must exist for a day with no demand data yet."""
    cfg, load, _ = small_world
    known = load[load.index < pd.Timestamp("2023-06-14 03:00", tz="UTC")]
    target = hourly_utc_range("2023-06-15", "2023-06-16", cfg.tz)
    lf = F.lag_features(known, target, 3, cfg.tz)
    assert lf.notna().all().all()


def test_counterfactual_features_have_no_demand(cfg):
    assert not set(F.LAGS) & set(F.CF_FEATURES)


def test_saving_with_range_maths():
    r = savings.saving_with_range(95.0, 100.0, sd=0.0, df=5, level=0.9)
    assert r["saving_pct"] == pytest.approx(5.0) and r["low_pct"] == pytest.approx(5.0)
    r = savings.saving_with_range(95.0, 100.0, sd=0.01, df=5, level=0.9)
    assert r["low_pct"] < 5.0 < r["high_pct"]


def test_sun_elevation():
    idx = pd.DatetimeIndex(["2023-06-21 11:00", "2023-12-21 23:00"], tz="UTC")
    e = sun_elevation_deg(idx, 51.0, 10.0)
    assert 55 < e[0] < 65 and e[1] < -40


@pytest.mark.slow
def test_gbm_save_load_roundtrip(small_world, tmp_path):
    cfg, load, weather = small_world
    X = F.build(cfg, load.index[:24 * 200], weather)
    m = operational.GBMModel(F.CF_FEATURES, {"n_estimators": 50, "verbose": -1}, [1], [0.05, 0.95])
    m.fit(X, load.reindex(X.index))
    m.save(tmp_path / "m")
    m2 = operational.GBMModel.load(tmp_path / "m")
    assert np.allclose(m.predict(X), m2.predict(X))
    q = m2.predict_quantiles(X, center=m2.predict(X))
    assert (q["q05"] <= m2.predict(X) + 1e-9).all() and (q["q95"] >= m2.predict(X) - 1e-9).all()


