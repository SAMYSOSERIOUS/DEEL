"""The honest forecast range, and the check against the official yearly figures."""
import numpy as np
import pandas as pd
import pytest

from desave.analysis import official
from desave.models import calibration


def _forecasts(days: int, true_spread: float, model_spread: float, backfilled: bool = False, seed: int = 1):
    """Fake saved forecasts: the model claims a narrow range, reality is wider."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-08-01", periods=24 * days, freq="h", tz="UTC")
    f = pd.Series(50_000.0, index=idx)
    actual = f * (1 + rng.normal(0, true_spread, len(idx)))
    half = 1.645 * model_spread * f
    fc = pd.DataFrame({"forecast_mwh": f, "q05_mwh": f - half, "q95_mwh": f + half,
                       "target_date": idx.strftime("%Y-%m-%d"), "backfilled": backfilled}, index=idx)
    return fc, actual


def test_range_widens_when_too_narrow():
    fc, actual = _forecasts(40, true_spread=0.03, model_spread=0.02)
    c = calibration.compute(fc, actual)
    assert c["coverage_before"] < 85
    assert 1.3 < c["scale"] < 1.8                     # about 0.03 / 0.02
    assert c["coverage_after"] >= 90


def test_range_left_alone_when_honest():
    fc, actual = _forecasts(40, true_spread=0.02, model_spread=0.025)
    assert calibration.compute(fc, actual)["scale"] == 1.0


def test_needs_a_week_of_data():
    fc, actual = _forecasts(5, true_spread=0.05, model_spread=0.01)
    assert calibration.compute(fc, actual)["scale"] == 1.0


def test_prefers_real_forecasts_over_backfilled():
    real, a1 = _forecasts(10, true_spread=0.04, model_spread=0.02)
    back, a2 = _forecasts(30, true_spread=0.01, model_spread=0.02, backfilled=True, seed=2)
    back.index = back.index - pd.Timedelta(days=30)
    back["target_date"] = back.index.strftime("%Y-%m-%d")
    a2.index = back.index
    c = calibration.compute(pd.concat([back, real]), pd.concat([a2, a1]))
    assert c["days_used"] == 10 and c["scale"] > 1.5


def test_apply_keeps_raw_range():
    df = pd.DataFrame({"forecast_mwh": [100.0], "q05_mwh": [90.0], "q95_mwh": [104.0]})
    out = calibration.apply(df, 1.5)
    assert out["q05_mwh"].iloc[0] == pytest.approx(85.0) and out["q95_mwh"].iloc[0] == pytest.approx(106.0)
    assert out["q05_raw_mwh"].iloc[0] == 90.0 and out["range_scale"].iloc[0] == 1.5


def test_old_forecasts_without_raw_columns_work():
    fc, actual = _forecasts(20, true_spread=0.03, model_spread=0.02)
    assert "q05_raw_mwh" not in fc
    assert calibration.compute(fc, actual)["scale"] > 1.0


def test_official_check(cfg):
    idx = pd.date_range("2021-01-01", "2024-01-01", freq="h", tz="Europe/Berlin", inclusive="left").tz_convert("UTC")
    year = idx.tz_convert("Europe/Berlin").year
    target = {2021: 504.5, 2022: 484.2, 2023: 456.8}        # TWh, so ours should match exactly
    load = pd.Series([target[y] * 1e6 / (year == y).sum() for y in year], index=idx)
    res = official.check(cfg, load)
    rows = {r["year"]: r for r in res["rows"]}
    assert rows[2022]["ours_twh"] == pytest.approx(484.2, abs=0.05)
    assert rows[2022]["ours_change_pct"] == pytest.approx(-4.0, abs=0.05)
    assert rows[2023]["diff_pct"] == pytest.approx(0, abs=0.01)
    assert res["matches"] is True and len(res["sources"]) == 2


def test_official_check_skips_incomplete_years(cfg):
    idx = pd.date_range("2023-01-01", "2023-06-01", freq="h", tz="UTC")
    res = official.check(cfg, pd.Series(50_000.0, index=idx))
    assert all(r["ours_twh"] is None for r in res["rows"])
