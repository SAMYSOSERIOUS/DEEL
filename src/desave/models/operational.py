"""The daily "tomorrow, hour by hour" model.

Unlike the "no crisis" model, this one MAY use recent demand (a week ago, two
weeks ago, the level of recent days), because its only job is to be accurate
tomorrow. It is retrained once a month on all data.

Safety gate: a new model only replaces the old one if, on the last 4 weeks
(which it did NOT see in training), it beats the "same week last year" baseline.
"""
from __future__ import annotations

import json
import logging
import shutil

import numpy as np
import pandas as pd

from .. import features as F
from ..config import Settings
from . import baseline
from .gbm import GBMModel

log = logging.getLogger(__name__)


def folder(cfg: Settings):
    return cfg.path("models") / "operational"


def new_model(cfg: Settings) -> GBMModel:
    return GBMModel(F.OP_FEATURES, dict(cfg["model"]["lightgbm"]), list(cfg["model"]["random_seeds"]),
                    list(cfg["model"]["quantiles"]))


def mape(a: pd.Series, p: pd.Series) -> float:
    ok = a.notna() & p.notna()
    return float(100 * np.mean(np.abs(a[ok] - p[ok]) / a[ok])) if ok.any() else float("nan")


def gate_check(cfg: Settings, X: pd.DataFrame, load: pd.Series, end: pd.Timestamp | None = None) -> dict:
    """Train without the last N days, then compare with the baseline on those days."""
    days = cfg["production"]["gate_window_days"]
    end = end or load.dropna().index.max() + pd.Timedelta(hours=1)
    start = end - pd.Timedelta(days=days)
    train = X.index < start
    test = (X.index >= start) & (X.index < end)
    m = new_model(cfg)
    m.quantiles = []  # not needed for the check
    m.fit(X[train], load.reindex(X.index)[train])
    a = load.reindex(X.index[test])
    pred = m.predict(X[test])
    base = baseline.same_week_last_year(load, X.index[test], cfg.tz)
    res = {"window_start": str(start), "window_end": str(end), "model_mape": mape(a, pred),
           "baseline_mape": mape(a, base)}
    res["passed"] = bool(res["model_mape"] < res["baseline_mape"] - cfg["production"]["gate_tolerance"])
    return res


def train(cfg: Settings, load: pd.Series, weather: pd.DataFrame, force: bool = False) -> dict:
    X = F.build(cfg, load.index, weather, load=load)
    gate = gate_check(cfg, X, load)
    log.info("Gate: model %.2f%% vs baseline %.2f%% -> %s", gate["model_mape"], gate["baseline_mape"],
             "pass" if gate["passed"] else "FAIL")
    out = folder(cfg)
    current = out / "current"
    if not gate["passed"] and current.exists() and not force:
        log.warning("New model did not beat the baseline on the last 4 weeks; keeping the old model.")
        return {"promoted": False, "gate": gate}
    m = new_model(cfg)
    m.fit(X, load.reindex(X.index))
    m.meta.update({"retrained_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="seconds"), "gate": gate})
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    archive = out / "archive" / stamp
    if archive.exists():
        shutil.rmtree(archive)
    m.save(archive)
    if current.exists():
        shutil.rmtree(current)
    shutil.copytree(archive, current)
    _keep_last(out / "archive", 6)
    (cfg.path("results") / "last_retrain.json").write_text(json.dumps(m.meta, indent=2, default=str))
    return {"promoted": True, "gate": gate, "meta": m.meta}


def _keep_last(folder_, n: int) -> None:
    olds = sorted(p for p in folder_.glob("*") if p.is_dir())
    for p in olds[:-n]:
        shutil.rmtree(p)


def load_current(cfg: Settings) -> GBMModel | None:
    f = folder(cfg) / "current"
    return GBMModel.load(f) if (f / "meta.json").exists() else None


def forecast(cfg: Settings, model: GBMModel, hours: pd.DatetimeIndex, weather: pd.DataFrame,
             load: pd.Series) -> pd.DataFrame:
    X = F.build(cfg, hours, weather, load=load)
    pred = model.predict(X)
    q = model.predict_quantiles(X, center=pred)
    df = pd.DataFrame({"forecast_mwh": pred}).join(q)
    df["baseline_mwh"] = baseline.same_week_last_year(load, hours, cfg.tz).to_numpy()
    df.columns = [c if not c.startswith("q") else f"{c}_mwh" for c in df.columns]
    return df
