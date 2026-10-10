"""The "what would demand have been without the crisis" model, and its tests
on normal winters where we already know the right answer.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from .. import features as F
from ..config import Settings
from ..timeutils import winter_bounds
from . import baseline
from .gbm import GBMModel

log = logging.getLogger(__name__)


def build_feature_table(cfg: Settings, load: pd.Series, weather: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Features for every hour with demand data: (real world, normal world with Covid switched off)."""
    idx = load.index
    X = F.build(cfg, idx, weather)
    Xn = X.copy()
    Xn["covid"] = 0.0
    return X, Xn


def local_cutoff(cfg: Settings, day: str) -> pd.Timestamp:
    return pd.Timestamp(day).tz_localize(cfg.tz).tz_convert("UTC")


def make_model(cfg: Settings, seeds: list[int] | None = None, quantiles: list[float] | None = None,
               features: list[str] | None = None) -> GBMModel:
    return GBMModel(features or F.CF_FEATURES, dict(cfg["model"]["lightgbm"]),
                    seeds or list(cfg["model"]["random_seeds"]), quantiles or [])


def fit(cfg: Settings, X: pd.DataFrame, load: pd.Series, train_end: pd.Timestamp,
        covid: str = "flag", seeds: list[int] | None = None) -> GBMModel:
    """Train on all hours before ``train_end``. ``covid='drop'`` leaves lockdown periods out entirely."""
    m = (X.index < train_end) & load.reindex(X.index).notna().to_numpy()
    if covid == "drop":
        m &= (X["covid"] == 0).to_numpy()
    model = make_model(cfg, seeds)
    model.fit(X.loc[m], load.reindex(X.index)[m])
    model.meta.update({"cutoff": str(train_end), "covid_handling": covid})
    return model


def season_horizon_years(cutoff: pd.Timestamp, winter: str, cfg: Settings) -> float:
    """How far ahead of the training data a winter starts, in years."""
    ws, _ = winter_bounds(winter, cfg["analysis"]["winter_months"], cfg.tz)
    return max(0.0, (ws - cutoff) / pd.Timedelta(days=365.25))


def backtest_normal_winters(cfg: Settings, load: pd.Series, X: pd.DataFrame, Xn: pd.DataFrame,
                            seeds: list[int] | None = None, max_gap: int = 3) -> tuple[pd.DataFrame, dict]:
    """Pretend each normal winter is unknown, forecast it from older data only, and score it.

    Each winter is tested from several distances (0 = trained up to the
    September just before; 1 = trained up to the September a year earlier, ...),
    because the crisis winters 2023/24 onwards are also forecast from far away.
    """
    months = cfg["analysis"]["winter_months"]
    start = load.index.min()
    rows, daily = [], {}
    refs = cfg["analysis"]["reference_years"]
    for w in cfg["analysis"]["normal_test_winters"]:
        ws, we = winter_bounds(w, months, cfg.tz)
        y0 = int(w[:4])
        for gap in range(0, max_gap + 1):
            cutoff = local_cutoff(cfg, f"{y0 - gap}-09-01")
            if cutoff - start < pd.Timedelta(days=365):
                continue  # need at least one full year to learn from
            model = fit(cfg, X, load, cutoff, seeds=seeds)
            m = (Xn.index >= ws) & (Xn.index < we)
            act = load.reindex(Xn.index[m])
            pred = model.predict(Xn.loc[m])
            ok = act.notna()
            act, pred = act[ok], pred[ok]
            b1 = baseline.same_week_last_year(load, act.index, cfg.tz)
            # a fair "average of normal years" baseline only uses years before the test winter
            b2 = baseline.pre_crisis_average(load, act.index, list(range(start.year, y0)) or refs, cfg.tz)
            h = season_horizon_years(cutoff, w, cfg)
            rows.append({
                "winter": w, "gap_years": gap, "horizon_years": round(h, 3), "cutoff": str(cutoff.date()),
                "hours": int(len(act)),
                "error_pct": 100 * (act.sum() / pred.sum() - 1),
                "hourly_mape": 100 * float(np.mean(np.abs(act - pred) / act)),
                "daily_mape": _daily_mape(act, pred, cfg),
                "baseline_last_year_error_pct": _total_err(act, b1),
                "baseline_last_year_hourly_mape": _mape(act, b1),
                "baseline_avg_years_error_pct": _total_err(act, b2),
            })
            if gap == 0:
                daily[w] = pd.DataFrame({"actual": act, "model": pred, "baseline": b1})
            log.info("Backtest %s gap %d: total error %.2f%%", w, gap, rows[-1]["error_pct"])
    return pd.DataFrame(rows), daily


def _mape(a: pd.Series, b: pd.Series) -> float:
    ok = a.notna() & b.notna()
    return 100 * float(np.mean(np.abs(a[ok] - b[ok]) / a[ok])) if ok.any() else float("nan")


def _total_err(a: pd.Series, b: pd.Series) -> float:
    ok = a.notna() & b.notna()
    return 100 * float(a[ok].sum() / b[ok].sum() - 1) if ok.any() else float("nan")


def _daily_mape(a: pd.Series, p: pd.Series, cfg: Settings) -> float:
    from ..timeutils import local_dates
    ld = local_dates(a.index, cfg.tz)
    da, dp = a.groupby(ld).sum(), p.groupby(ld).sum()
    return 100 * float(np.mean(np.abs(da - dp) / da))


# ------------------------------------------------------------- error model
def error_model(backtests: pd.DataFrame) -> dict:
    """How big the whole-winter error gets the further ahead we forecast.

    Fit: error² ≈ a + b × years_ahead (a straight line through the squared
    errors, never below zero). Typical error at horizon h is √(a + b·h).
    Also: the average error per year ahead ("drift"), used by the trend variant.
    """
    e = backtests["error_pct"].to_numpy() / 100
    h = backtests["horizon_years"].to_numpy()
    A = np.vstack([np.ones_like(h), h]).T
    coef, *_ = np.linalg.lstsq(A, e ** 2, rcond=None)
    a, b = float(max(coef[0], 1e-6)), float(max(coef[1], 0.0))
    # drift: average error grows by this much per year ahead (straight line through 0 at h=0 intercept)
    drift_coef, *_ = np.linalg.lstsq(A, e, rcond=None)
    return {"a": a, "b": b, "n": int(len(e)), "drift_intercept": float(drift_coef[0]),
            "drift_per_year": float(drift_coef[1]),
            "rms_by_gap": {int(g): float(np.sqrt(np.mean((d["error_pct"] / 100) ** 2)))
                           for g, d in backtests.groupby("gap_years")},
            "mean_by_gap": {int(g): float(d["error_pct"].mean() / 100) for g, d in backtests.groupby("gap_years")}}


def error_sd(em: dict, horizon_years: float) -> float:
    return float(np.sqrt(em["a"] + em["b"] * horizon_years))
