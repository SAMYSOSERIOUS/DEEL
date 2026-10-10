"""LightGBM demand model.

* Learns log(demand), so effects act as percentages (a holiday cuts demand by
  a share, not by a fixed amount), which also keeps forecasts positive.
* Trains a few copies with different random seeds and averages them, so the
  answer doesn't change just because of the random start.
* Optional quantile models give the range shown on the web page.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd


@dataclass
class GBMModel:
    features: list[str]
    params: dict
    seeds: list[int] = field(default_factory=lambda: [1])
    quantiles: list[float] = field(default_factory=list)
    boosters: list[lgb.Booster] = field(default_factory=list)
    q_boosters: dict[float, lgb.Booster] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def _clean_params(self) -> dict:
        p = dict(self.params)
        p.pop("n_estimators", None)
        return p

    def fit(self, X: pd.DataFrame, y: pd.Series, sample_weight: np.ndarray | None = None) -> "GBMModel":
        ok = y.notna() & (y > 0)
        X, y = X.loc[ok, self.features], np.log(y[ok])
        w = None if sample_weight is None else np.asarray(sample_weight)[ok.to_numpy()]
        n = int(self.params.get("n_estimators", 1000))
        self.boosters = []
        for seed in self.seeds:
            p = {**self._clean_params(), "objective": "regression", "seed": seed,
                 "bagging_seed": seed, "feature_fraction_seed": seed}
            ds = lgb.Dataset(X, label=y, weight=w, free_raw_data=False)
            self.boosters.append(lgb.train(p, ds, num_boost_round=n))
        self.q_boosters = {}
        for q in self.quantiles:
            p = {**self._clean_params(), "objective": "quantile", "alpha": q, "seed": self.seeds[0]}
            ds = lgb.Dataset(X, label=y, weight=w, free_raw_data=False)
            self.q_boosters[q] = lgb.train(p, ds, num_boost_round=max(300, n // 2))
        self.meta.update({"train_rows": int(len(y)), "train_start": str(X.index.min()),
                          "train_end": str(X.index.max())})
        return self

    def predict(self, X: pd.DataFrame) -> pd.Series:
        pred = np.mean([b.predict(X[self.features]) for b in self.boosters], axis=0)
        return pd.Series(np.exp(pred), index=X.index, name="pred")

    def predict_quantiles(self, X: pd.DataFrame, center: pd.Series | None = None) -> pd.DataFrame:
        out = {}
        for q, b in sorted(self.q_boosters.items()):
            out[f"q{int(round(q * 100)):02d}"] = np.exp(b.predict(X[self.features]))
        df = pd.DataFrame(out, index=X.index)
        if center is not None and len(df.columns) == 2:  # make sure the range always contains the forecast
            lo, hi = df.columns
            df[lo] = np.minimum(df[lo], center)
            df[hi] = np.maximum(df[hi], center)
        return df

    def feature_importance(self) -> pd.Series:
        imp = np.mean([b.feature_importance("gain") for b in self.boosters], axis=0)
        return pd.Series(imp, index=self.features).sort_values(ascending=False)

    # ------------------------------------------------------------ save/load
    def save(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(self.boosters):
            b.save_model(str(folder / f"model_{i}.txt"))
        for q, b in self.q_boosters.items():
            b.save_model(str(folder / f"quantile_{q:.2f}.txt"))
        (folder / "meta.json").write_text(json.dumps({
            "features": self.features, "params": self.params, "seeds": self.seeds,
            "quantiles": self.quantiles, "meta": self.meta}, indent=2, default=str))

    @classmethod
    def load(cls, folder: Path) -> "GBMModel":
        m = json.loads((folder / "meta.json").read_text())
        obj = cls(m["features"], m["params"], m["seeds"], m["quantiles"], meta=m.get("meta", {}))
        obj.boosters = [lgb.Booster(model_file=str(folder / f"model_{i}.txt")) for i in range(len(m["seeds"]))]
        obj.q_boosters = {q: lgb.Booster(model_file=str(folder / f"quantile_{q:.2f}.txt")) for q in m["quantiles"]}
        return obj
