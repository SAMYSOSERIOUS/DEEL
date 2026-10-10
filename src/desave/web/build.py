"""Build the static web page (site/index.html + site/data.json).

Static on purpose: it can be hosted for free on GitHub Pages, and there is no
server to keep running. The daily job rebuilds it.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from .. import dataset
from ..config import Settings
from ..timeutils import local_dates

log = logging.getLogger(__name__)
TEMPLATE = Path(__file__).with_name("template.html")


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _r(x, n=1):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), n)


def collect(cfg: Settings) -> dict:
    from ..pipeline import daily
    res_dir = cfg.path("results")
    data: dict = {"generated_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
                  "demo": bool(cfg.get("demo")), "tz": cfg.tz}
    data["status"] = _read_json(res_dir / "last_run.json")
    retrain = _read_json(res_dir / "last_retrain.json")
    data["retrain"] = {"date": str(retrain.get("retrained_utc", ""))[:10], "gate": retrain.get("gate", {})}

    fc = daily.all_forecasts(cfg)
    try:
        load = dataset.load_series(cfg, check=False)
    except FileNotFoundError:
        load = pd.Series(dtype="float64")
    if len(fc):
        last_target = fc["target_date"].max()
        t = fc[fc["target_date"] == last_target]
        loc = t.index.tz_convert(cfg.tz)
        data["tomorrow"] = {
            "date": last_target, "issued_utc": str(t["issued_utc"].iloc[0]),
            "weather_source": str(t["weather_source"].iloc[0]),
            "hours": [x.strftime("%Y-%m-%dT%H:%M") for x in loc],
            "hour_labels": [x.strftime("%H:%M") for x in loc],
            "forecast_gw": [_r(v / 1000, 2) for v in t["forecast_mwh"]],
            "low_gw": [_r(v / 1000, 2) for v in t.get("q05_mwh", t["forecast_mwh"])],
            "high_gw": [_r(v / 1000, 2) for v in t.get("q95_mwh", t["forecast_mwh"])],
            "baseline_gw": [_r(v / 1000, 2) for v in t["baseline_mwh"]],
            "total_gwh": _r(t["forecast_mwh"].sum() / 1000, 0),
            "total_low_gwh": _r(t.get("q05_mwh", t["forecast_mwh"]).sum() / 1000, 0),
            "total_high_gwh": _r(t.get("q95_mwh", t["forecast_mwh"]).sum() / 1000, 0),
            "peak_gw": _r(t["forecast_mwh"].max() / 1000, 1),
            "peak_hour": loc[int(np.argmax(t["forecast_mwh"].to_numpy()))].strftime("%H:%M"),
        }
        past = fc[fc["target_date"] < last_target]
        if len(past) and len(load):
            end = past.index.max()
            start = end - pd.Timedelta(days=cfg["production"]["site_days"])
            p = past[past.index > start]
            a = load.reindex(p.index)
            data["last30"] = {
                "time": [x.strftime("%Y-%m-%dT%H:%M") for x in p.index.tz_convert(cfg.tz)],
                "actual_gw": [_r(v / 1000, 2) for v in a],
                "forecast_gw": [_r(v / 1000, 2) for v in p["forecast_mwh"]],
                "baseline_gw": [_r(v / 1000, 2) for v in p["baseline_mwh"]],
                "backfilled_until": (str(p.index[p["backfilled"].astype(bool)].tz_convert(cfg.tz).max())[:16]
                                     if "backfilled" in p and p["backfilled"].astype(bool).any() else None),
            }
    scores_f = res_dir / "scores.csv"
    if scores_f.exists():
        sc = pd.read_csv(scores_f).tail(cfg["production"]["site_days"])
        if len(sc):
            data["scores"] = {
                "days": int(len(sc)), "model_mape": _r(sc["model_mape"].mean(), 2),
                "baseline_mape": _r(sc["baseline_mape"].mean(), 2),
                "inside_range_pct": _r(sc["inside_range_pct"].mean(), 0),
                "backfilled_days": int(sc["backfilled"].astype(bool).sum()) if "backfilled" in sc else 0,
                "table": sc[["date", "model_mape", "baseline_mape", "model_daily_error_pct", "inside_range_pct"]]
                .round(2).iloc[::-1].to_dict(orient="records"),
                "running_model": [_r(v, 2) for v in sc["model_mape"].expanding().mean()],
                "running_baseline": [_r(v, 2) for v in sc["baseline_mape"].expanding().mean()],
                "dates": sc["date"].tolist(),
            }
    sv = _read_json(res_dir / "savings.json")
    if sv:
        data["savings"] = {
            "headline": sv.get("headline"), "verdict": sv.get("verdict"),
            "winters": [{k: (_r(v, 2) if isinstance(v, float) else v) for k, v in r.items()} for r in sv["winters"]],
            "checks": [{k: (_r(v, 2) if isinstance(v, float) else v) for k, v in r.items()}
                       for r in sv.get("check_winters", [])],
            "dark": next((r for r in sv.get("variants", {}).get("dark hours only (no solar)", [])
                          if r["winter"] == "2022/23"), None),
            "generated": sv.get("generated_utc", "")[:10],
        }
    if len(load):
        data["data_until"] = str(load.index.max().tz_convert(cfg.tz))[:16]
        data["data_days"] = int(len(set(local_dates(load.index, cfg.tz))))
    return data


def build(cfg: Settings) -> Path:
    data = collect(cfg)
    site = cfg.path("site")
    (site / "data.json").write_text(json.dumps(data, default=str))
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(data, default=str))
    out = site / "index.html"
    out.write_text(html, encoding="utf-8")
    fig = cfg.path("reports") / "figures"
    if fig.exists():
        dst = site / "figures"
        dst.mkdir(exist_ok=True)
        for f in fig.glob("*.png"):
            shutil.copy2(f, dst / f.name)
    report = cfg.path("reports") / "REPORT.md"
    if report.exists():
        shutil.copy2(report, site / "REPORT.md")
    log.info("Web page written to %s", out)
    return out
