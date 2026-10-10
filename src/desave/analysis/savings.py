"""The main question: after adjusting for weather and holidays, how much less
electricity did Germany use in winter 2022/23 than a normal winter, and has
demand come back since?

Saving = 1 − actual demand ÷ "no crisis" forecast. Positive = used less.

The range around each saving comes from how wrong the same method was on
normal winters (2016/17-2019/20), forecast from the same distance ahead.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import stats

from .. import dataset
from ..config import Settings
from ..models import baseline
from ..models import counterfactual as CF
from ..timeutils import local_dates, local_index, winter_bounds

log = logging.getLogger(__name__)
MWH_PER_TWH = 1e6


@dataclass
class Inputs:
    load: pd.Series
    weather: pd.DataFrame
    X: pd.DataFrame
    Xn: pd.DataFrame


def prepare(cfg: Settings) -> Inputs:
    load = dataset.load_series(cfg)
    weather = dataset.weather_observed(cfg)
    X, Xn = CF.build_feature_table(cfg, load, weather)
    return Inputs(load, weather, X, Xn)


# --------------------------------------------------------------- core maths
def saving_with_range(actual_sum: float, pred_sum: float, sd: float, df: int, level: float) -> dict:
    """Saving and its range. In a normal winter actual/forecast = 1 + e, with e ~ typical error."""
    ratio = actual_sum / pred_sum
    tq = float(stats.t.ppf(0.5 + level / 2, df))
    e_hi, e_lo = tq * sd, -tq * sd
    return {
        "saving_pct": 100 * (1 - ratio),
        "low_pct": 100 * (1 - ratio / (1 + e_lo)),
        "high_pct": 100 * (1 - ratio / (1 + e_hi)),
        "typical_error_pct": 100 * sd,
    }


def winter_rows(cfg: Settings, actual: pd.Series, pred: pd.Series, em: dict, cutoff: pd.Timestamp,
                months: list[int] | None = None, mask: pd.Series | None = None,
                winters: list[str] | None = None) -> pd.DataFrame:
    months = months or cfg["analysis"]["winter_months"]
    level = cfg["analysis"]["interval_level"]
    df_t = max(em["n"] - 2, 2)
    rows = []
    loc_month = pd.Series(local_index(actual.index, cfg.tz).month, index=actual.index)
    for w in winters or cfg["analysis"]["crisis_winters"]:
        ws, we = winter_bounds(w, cfg["analysis"]["winter_months"], cfg.tz)
        sel = (actual.index >= ws) & (actual.index < we) & loc_month.isin(months).to_numpy()
        if mask is not None:
            sel &= mask.reindex(actual.index).fillna(False).to_numpy()
        a, p = actual[sel], pred.reindex(actual.index)[sel]
        ok = a.notna() & p.notna()
        a, p = a[ok], p[ok]
        possible = pd.date_range(ws, we, freq="h", inclusive="left")
        possible = possible[pd.Index(local_index(possible, cfg.tz).month).isin(months)]
        if mask is not None:
            possible = possible[mask.reindex(possible).fillna(False).to_numpy()]
        coverage = len(a) / max(len(possible), 1)
        if len(a) == 0:
            continue
        h = CF.season_horizon_years(cutoff, w, cfg)
        r = saving_with_range(a.sum(), p.sum(), CF.error_sd(em, h), df_t, level)
        rows.append({"winter": w, "complete": coverage >= 0.98, "coverage": round(coverage, 4),
                     "hours": int(len(a)), "years_ahead": round(h, 2),
                     "actual_twh": a.sum() / MWH_PER_TWH, "expected_twh": p.sum() / MWH_PER_TWH,
                     "saving_twh": (p.sum() - a.sum()) / MWH_PER_TWH, **r})
    return pd.DataFrame(rows)


def check_winter_rows(backtests: pd.DataFrame, em: dict, cfg: Settings) -> pd.DataFrame:
    """The normal test winters shown like crisis winters (their 'saving' should be about 0)."""
    level = cfg["analysis"]["interval_level"]
    df_t = max(em["n"] - 2, 2)
    out = []
    for _, r in backtests[backtests["gap_years"] == 0].iterrows():
        ratio = 1 + r["error_pct"] / 100
        rng = saving_with_range(ratio, 1.0, CF.error_sd(em, r["horizon_years"]), df_t, level)
        out.append({"winter": r["winter"], "kind": "check (normal winter)", **rng})
    return pd.DataFrame(out)


# ------------------------------------------------------------------- extras
def decomposition(cfg: Settings, actual: pd.Series, pred: pd.Series, winters_table: pd.DataFrame) -> pd.DataFrame:
    """Split the raw drop vs. normal pre-crisis winters into 'weather & calendar' and 'saving'."""
    months = cfg["analysis"]["winter_months"]

    def mean_in(series, w):
        ws, we = winter_bounds(w, months, cfg.tz)
        s = series[(series.index >= ws) & (series.index < we)]
        both = s.notna() & actual.reindex(s.index).notna()
        return float(s[both].mean())

    refs = cfg["analysis"]["normal_test_winters"]
    ref_actual = np.mean([mean_in(actual, w) for w in refs])
    ref_pred = np.mean([mean_in(pred, w) for w in refs])
    rows = []
    for _, r in winters_table.iterrows():
        w = r["winter"]
        raw = 100 * (mean_in(actual, w) / ref_actual - 1)
        weather_cal = 100 * (mean_in(pred, w) / ref_pred - 1)
        rows.append({"winter": w, "raw_change_vs_normal_winters_pct": raw,
                     "explained_by_weather_and_calendar_pct": weather_cal,
                     "saving_pct": r["saving_pct"],
                     "unexplained_rest_pct": raw - weather_cal + r["saving_pct"]})
    return pd.DataFrame(rows)


def monthly_savings(cfg: Settings, actual: pd.Series, pred: pd.Series, since: str = "2019-01") -> pd.DataFrame:
    ok = actual.notna() & pred.reindex(actual.index).notna()
    a, p = actual[ok], pred.reindex(actual.index)[ok]
    month = pd.Index(local_index(a.index, cfg.tz).tz_localize(None).to_period("M"), name="month")
    g = pd.DataFrame({"actual": a.groupby(month).sum(), "expected": p.groupby(month).sum(),
                      "hours": a.groupby(month).size()})
    g = g[g.index >= pd.Period(since, "M")]
    g["saving_pct"] = 100 * (1 - g["actual"] / g["expected"])
    g.index = g.index.astype(str)
    return g


def profile_savings(cfg: Settings, actual: pd.Series, pred: pd.Series, X: pd.DataFrame, winter: str) -> dict:
    """When in the day / week did the saving happen? (hints at who saved)"""
    ws, we = winter_bounds(winter, cfg["analysis"]["winter_months"], cfg.tz)
    sel = (actual.index >= ws) & (actual.index < we)
    a, p = actual[sel], pred.reindex(actual.index)[sel]
    ok = a.notna() & p.notna()
    a, p, x = a[ok], p[ok], X.reindex(a[ok].index)
    hour = x["hour"].to_numpy()
    by_hour = {int(h): float(100 * (1 - a[hour == h].sum() / p[hour == h].sum())) for h in range(24)}
    workday = ((x["dow"] < 5) & (x["holiday_share"] < 0.5)).to_numpy()
    return {
        "by_hour": by_hour,
        "workdays_pct": float(100 * (1 - a[workday].sum() / p[workday].sum())),
        "weekends_holidays_pct": float(100 * (1 - a[~workday].sum() / p[~workday].sum())),
        "workday_daytime_pct": float(100 * (1 - a[workday & (hour >= 7) & (hour < 18)].sum()
                                            / p[workday & (hour >= 7) & (hour < 18)].sum())),
    }


def industry_link(monthly: pd.DataFrame, industry: pd.DataFrame, cutoff: str, codes: list[str]) -> dict:
    if industry is None or industry.empty:
        return {"available": False}
    have = [c for c in codes if c in industry.columns]
    if not have:
        return {"available": False}
    ind = industry[have].mean(axis=1).rename("energy_intensive_index")
    df = monthly.join(ind, how="inner")
    df = df[df.index >= cutoff[:7]]
    if len(df) < 6:
        return {"available": False}
    df["industry_change_pct"] = df["energy_intensive_index"] - 100.0  # index: 2021 = 100
    corr = float(df["saving_pct"].corr(-df["industry_change_pct"]))
    return {"available": True, "months": int(len(df)), "series": have,
            "correlation_saving_vs_industry_drop": round(corr, 3),
            "industry_change_first_crisis_winter_pct": float(
                df.loc[(df.index >= cutoff[:4] + "-10") & (df.index <= str(int(cutoff[:4]) + 1) + "-03"),
                       "industry_change_pct"].mean()),
            "table": df[["saving_pct", "industry_change_pct"]].round(2).to_dict(orient="index")}


# ---------------------------------------------------------------------- run
def run(cfg: Settings, inputs: Inputs | None = None, seeds: list[int] | None = None,
        industry: pd.DataFrame | None = None) -> dict:
    inp = inputs or prepare(cfg)
    load, X, Xn = inp.load, inp.X, inp.Xn
    A = cfg["analysis"]
    main_cut = CF.local_cutoff(cfg, A["main_cutoff"])

    log.info("Step 4: testing the method on normal winters")
    backtests, bt_daily = CF.backtest_normal_winters(cfg, load, X, Xn, seeds=seeds)
    em = CF.error_model(backtests)

    log.info("Step 5: forecasting the crisis winters as if nothing happened")
    preds, models = {}, {}
    for c in sorted(set(A["cutoff_variants"]) | {A["main_cutoff"]}):
        cut = CF.local_cutoff(cfg, c)
        models[c] = CF.fit(cfg, X, load, cut, seeds=seeds)
        preds[c] = models[c].predict(Xn)
    pred = preds[A["main_cutoff"]]
    models[A["main_cutoff"]].save(cfg.path("models") / "counterfactual")

    main = winter_rows(cfg, load, pred, em, main_cut)
    checks = check_winter_rows(backtests, em, cfg)

    log.info("Step 6-8: ranges, other views and possible explanations")
    variants = {}
    for c, p in preds.items():
        variants[f"cutoff {c}"] = winter_rows(cfg, load, p, em, CF.local_cutoff(cfg, c))
    variants["Dec-Feb only"] = winter_rows(cfg, load, pred, em, main_cut, months=A["core_winter_months"])
    dark = pd.Series(X["sun_elev"] < A["dark_hours_sun_elevation_deg"], index=X.index)
    variants["dark hours only (no solar)"] = winter_rows(cfg, load, pred, em, main_cut, mask=dark)
    m_drop = CF.fit(cfg, X, load, main_cut, covid="drop", seeds=seeds)
    variants["lockdown months left out"] = winter_rows(cfg, load, m_drop.predict(Xn), em, main_cut)
    # On normal winters the model was off by (intercept + slope × years ahead) on average.
    # This view corrects the forecast for that known bias (mostly demand slowly falling before 2022).
    years_ahead = pd.Series(np.clip((X.index - main_cut) / pd.Timedelta(days=365.25), 0, None), index=X.index)
    bias = np.where(years_ahead > 0, em["drift_intercept"] + em["drift_per_year"] * years_ahead, 0.0)
    trend_pred = pred * (1 + bias)
    variants["corrected for known model bias (trend)"] = winter_rows(cfg, load, trend_pred, em, main_cut)
    variants["dark hours + bias corrected (most cautious)"] = winter_rows(cfg, load, trend_pred, em, main_cut,
                                                                          mask=dark)
    simple = baseline.pre_crisis_average(load, load.index, A["reference_years"], cfg.tz)
    variants["simple average of 2017-19 (no weather adjustment)"] = winter_rows(cfg, load, simple, em, main_cut)

    decomp = decomposition(cfg, load, pred, main)
    monthly = monthly_savings(cfg, load, pred)
    profile = profile_savings(cfg, load, pred, X, "2022/23") if "2022/23" in set(main["winter"]) else {}
    ind = industry_link(monthly, industry, A["main_cutoff"], A["industry_series"]) if industry is not None else {
        "available": False}

    complete = main[main["complete"]]
    head = main[main["winter"] == "2022/23"].iloc[0].to_dict() if (main["winter"] == "2022/23").any() else None
    latest = complete.iloc[-1].to_dict() if len(complete) else None
    results = {
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"),
        "data": {"first_hour_utc": str(load.index.min()), "last_hour_utc": str(load.index.max()),
                 "hours": int(load.notna().sum())},
        "settings": {"main_cutoff": A["main_cutoff"], "winter_months": A["winter_months"],
                     "interval_level": A["interval_level"]},
        "headline": head,
        "latest_complete_winter": latest,
        "verdict": verdict(head, latest, variants.get("dark hours only (no solar)")),
        "winters": main.to_dict(orient="records"),
        "check_winters": checks.to_dict(orient="records"),
        "backtests": backtests.to_dict(orient="records"),
        "error_model": em,
        "variants": {k: v.to_dict(orient="records") for k, v in variants.items()},
        "decomposition": decomp.to_dict(orient="records"),
        "monthly": monthly.reset_index().to_dict(orient="records"),
        "profile_2022_23": profile,
        "industry": ind,
        "feature_importance": models[A["main_cutoff"]].feature_importance().round(1).to_dict(),
    }
    out = cfg.path("results")
    (out / "savings.json").write_text(json.dumps(results, indent=2, default=_json_default))
    main.to_csv(out / "savings_by_winter.csv", index=False)
    backtests.to_csv(out / "backtests_normal_winters.csv", index=False)
    pd.concat({k: v.set_index("winter") for k, v in variants.items()}, names=["variant"]).to_csv(
        out / "savings_variants.csv")
    hourly = pd.DataFrame({"actual_mwh": load, "expected_no_crisis_mwh": pred,
                           "expected_trend_variant_mwh": trend_pred})
    hourly[hourly.index >= main_cut - pd.Timedelta(days=365)].to_parquet(out / "counterfactual_hourly.parquet")
    _save_backtest_daily(bt_daily, cfg)
    return results


def _save_backtest_daily(bt_daily: dict, cfg: Settings) -> None:
    frames = []
    for w, df in bt_daily.items():
        ld = local_dates(df.index, cfg.tz)
        d = df.groupby(ld).sum(min_count=20)
        d["winter"] = w
        frames.append(d)
    if frames:
        pd.concat(frames).to_csv(cfg.path("results") / "backtest_daily.csv")


def verdict(head: dict | None, latest: dict | None, dark: pd.DataFrame | None) -> str:
    """Plain-English answer, written from the numbers (never hand-typed)."""
    if not head:
        return "Not enough data yet to judge winter 2022/23."
    s = (f"In winter 2022/23 (October to March), Germany's grid delivered {head['saving_pct']:.1f}% less "
         f"electricity than the weather and calendar predicted (90% range {head['low_pct']:.1f}% to "
         f"{head['high_pct']:.1f}%), about {head['saving_twh']:.1f} TWh.")
    if head["low_pct"] <= 0:
        s += " The range includes zero, so we cannot be sure there was a real saving."
    if latest and latest["winter"] != "2022/23":
        if latest["low_pct"] > 0:
            s += (f" Demand has NOT fully come back: in {latest['winter']} it was still {latest['saving_pct']:.1f}% "
                  f"below the pre-crisis pattern (range {latest['low_pct']:.1f}% to {latest['high_pct']:.1f}%).")
        elif latest["high_pct"] < 0:
            s += f" By {latest['winter']} demand was back ABOVE the pre-crisis pattern ({-latest['saving_pct']:.1f}% more)."
        else:
            s += (f" By {latest['winter']} the gap was {latest['saving_pct']:.1f}% and its range includes zero, "
                  "so demand may have come back.")
    if dark is not None and len(dark) and (dark["winter"] == "2022/23").any():
        d = dark[dark["winter"] == "2022/23"].iloc[0]
        s += (f" Counting only dark hours (when rooftop solar can't hide demand), the 2022/23 saving was "
              f"{d['saving_pct']:.1f}%.")
    return s


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (pd.Timestamp, pd.Period)):
        return str(o)
    return str(o)

