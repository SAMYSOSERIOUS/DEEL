"""Build the static website: site/index.html, site/report.html, site/data.json.

Static on purpose: hosted free on GitHub Pages, nothing to keep running.
Everything the page needs ships with it (the 3D library and the map outline
are copied in), so no outside server can break it. Only the fonts come from
Google Fonts, with plain fallbacks if they don't load.
"""
from __future__ import annotations

import html
import json
import logging
import re
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

from .. import areas, dataset
from ..config import Settings
from ..timeutils import local_dates

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
TEMPLATE = HERE / "template.html"
GEO = HERE / "germany.json"
VENDOR = HERE / "vendor"


def _read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _r(x, n=1):
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) else round(x, n)


def _points(cfg: Settings) -> list[dict]:
    pts = cfg["weather_points"]
    total = sum(float(p["pop_k"]) for p in pts)
    return [{"name": p["name"], "lat": p["lat"], "lon": p["lon"], "weight": round(float(p["pop_k"]) / total, 4)} for p in pts]


def collect(cfg: Settings) -> dict:
    """Everything for the page: Germany at the top level (as before) plus each grid region under "areas"."""
    data = collect_area(cfg)
    data.update({"generated_utc": pd.Timestamp.now(tz="UTC").isoformat(timespec="minutes"),
                 "demo": bool(cfg.get("demo")), "tz": cfg.tz,
                 "geo": json.loads(GEO.read_text()) if GEO.exists() else None, "points": _points(cfg),
                 "hierarchy": areas.tree(cfg)})
    data["status"] = _read_json(cfg.path("results") / "last_run.json")
    data["areas"] = {}
    for code in areas.region_codes(cfg):
        try:
            data["areas"][code] = collect_area(areas.scoped(cfg, code))
        except Exception as e:  # noqa: BLE001 - a region without results yet just shows as empty
            log.warning("No website data for %s: %s", code, e)
            data["areas"][code] = {}
    return data


def collect_area(cfg: Settings) -> dict:
    from ..pipeline import daily
    res_dir = cfg.path("results")
    data: dict = {}
    retrain = _read_json(res_dir / "last_retrain.json")
    data["retrain"] = {"date": str(retrain.get("retrained_utc", ""))[:10], "gate": retrain.get("gate", {})}
    cal = _read_json(res_dir / "interval_calibration.json")
    if cal:
        data["range_calibration"] = cal

    fc = daily.all_forecasts(cfg)
    try:
        load = dataset.load_series(cfg, check=False)
    except FileNotFoundError:
        load = pd.Series(dtype="float64")
    if len(fc):
        last_target = fc["target_date"].max()
        t = fc[fc["target_date"] == last_target]
        loc = t.index.tz_convert(cfg.tz)
        q05 = t["q05_mwh"] if "q05_mwh" in t else t["forecast_mwh"]
        q95 = t["q95_mwh"] if "q95_mwh" in t else t["forecast_mwh"]
        data["tomorrow"] = {
            "date": last_target, "issued_utc": str(t["issued_utc"].iloc[0]),
            "weather_source": str(t["weather_source"].iloc[0]),
            "hours": [x.strftime("%Y-%m-%dT%H:%M") for x in loc],
            "hour_labels": [x.strftime("%H:%M") for x in loc],
            "forecast_gw": [_r(v / 1000, 2) for v in t["forecast_mwh"]],
            "low_gw": [_r(v / 1000, 2) for v in q05], "high_gw": [_r(v / 1000, 2) for v in q95],
            "baseline_gw": [_r(v / 1000, 2) for v in t["baseline_mwh"]],
            "total_gwh": _r(t["forecast_mwh"].sum() / 1000, 0),
            "total_low_gwh": _r(q05.sum() / 1000, 0), "total_high_gwh": _r(q95.sum() / 1000, 0),
            "peak_gw": _r(t["forecast_mwh"].max() / 1000, 1),
            "peak_hour": loc[int(np.argmax(t["forecast_mwh"].to_numpy()))].strftime("%H:%M"),
        }
        past = fc[fc["target_date"] < last_target]
        if len(past) and len(load):
            start = past.index.max() - pd.Timedelta(days=cfg["production"]["site_days"])
            p = past[past.index > start]
            a = load.reindex(p.index)
            bf = p["backfilled"].astype(bool) if "backfilled" in p else pd.Series(False, index=p.index)
            data["last30"] = {
                "time": [x.strftime("%Y-%m-%dT%H:%M") for x in p.index.tz_convert(cfg.tz)],
                "actual_gw": [_r(v / 1000, 2) for v in a],
                "forecast_gw": [_r(v / 1000, 2) for v in p["forecast_mwh"]],
                "baseline_gw": [_r(v / 1000, 2) for v in p["baseline_mwh"]],
                "backfilled_until": str(p.index[bf.to_numpy()].tz_convert(cfg.tz).max().strftime("%Y-%m-%dT%H:%M")) if bf.any() else None,
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
        clean = lambda r: {k: (_r(v, 2) if isinstance(v, float) else v) for k, v in r.items()}  # noqa: E731
        data["savings"] = {
            "headline": sv.get("headline"), "verdict": sv.get("verdict"),
            "winters": [clean(r) for r in sv.get("winters", [])],
            "checks": [clean(r) for r in sv.get("check_winters", [])],
            "dark": next((clean(r) for r in sv.get("variants", {}).get("dark hours only (no solar)", [])
                          if r["winter"] == "2022/23"), None),
            "generated": sv.get("generated_utc", "")[:10],
        }
        if sv.get("official_check"):
            data["official"] = sv["official_check"]
    if len(load):
        data["data_until"] = str(load.index.max().tz_convert(cfg.tz))[:16]
        data["data_days"] = int(len(set(local_dates(load.index, cfg.tz))))
    return data


# ------------------------------------------------------------------ report page
REPORT_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Netzlast Report</title>
<meta name="description" content="Full method, tests and results: did Germany really save electricity in the 2022 energy crisis?">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@600;700&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root { color-scheme: dark; --ground: #0A0D12; --panel: #11161D; --line: #1E2833; --ink: #ECEFF3; --ink-2: #A3AEBA; --muted: #7A8693; --gold: #F2B705; --gold-hi: #FFE38A;
  --display: "Barlow Condensed", "Arial Narrow", sans-serif; --body: Barlow, system-ui, sans-serif; --mono: "JetBrains Mono", ui-monospace, monospace; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { color-scheme: light; --ground: #F3F1EA; --panel: #FBFAF6; --line: #D9D5C9; --ink: #14181D; --ink-2: #4A525C; --muted: #6A717A; --gold: #A87A00; --gold-hi: #D9A100; } }
:root[data-theme="light"] { color-scheme: light; --ground: #F3F1EA; --panel: #FBFAF6; --line: #D9D5C9; --ink: #14181D; --ink-2: #4A525C; --muted: #6A717A; --gold: #A87A00; --gold-hi: #D9A100; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--ground); color: var(--ink); font: 17px/1.65 var(--body); }
a { color: var(--gold); } a:hover { color: var(--gold-hi); }
.flag { display: flex; height: 4px; } .flag span { flex: 1; }
.top { max-width: 900px; margin: 0 auto; padding: 16px 24px; display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; align-items: center; }
.top a.home { font: 700 20px/1 var(--display); letter-spacing: 0.06em; color: var(--ink); text-decoration: none; }
main { max-width: 900px; margin: 0 auto; padding: 8px 24px 64px; }
@media (max-width: 560px) { main, .top { padding-inline: 16px; } }
h1 { font: 700 clamp(36px, 6vw, 56px)/1.05 var(--display); margin: 16px 0 8px; text-wrap: balance; }
h2 { font: 600 32px/1.15 var(--display); margin: 48px 0 12px; padding-top: 20px; border-top: 1px solid var(--line); text-wrap: balance; }
h3 { font: 600 22px/1.2 var(--display); margin: 28px 0 8px; }
p, li { color: var(--ink-2); max-width: 70ch; }
strong { color: var(--ink); }
em { color: var(--muted); }
img { display: block; max-width: 100%; height: auto; margin: 16px 0; border-radius: 10px; background: #fff; padding: 8px; border: 1px solid var(--line); }
.tablebox { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; margin: 16px 0; }
table { border-collapse: collapse; width: 100%; font-size: 14px; font-variant-numeric: tabular-nums; }
th, td { padding: 9px 12px; border-bottom: 1px solid var(--line); text-align: right; white-space: nowrap; }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase; }
td { font-family: var(--mono); color: var(--ink); }
tr:last-child td { border-bottom: 0; }
code { font-family: var(--mono); font-size: 0.9em; }
</style>
</head>
<body>
<div class="flag" aria-hidden="true"><span style="background:#000"></span><span style="background:#DD0000"></span><span style="background:#FFCE00"></span></div>
<div class="top"><a class="home" href="index.html">NETZLAST WATCH</a><a href="index.html">← Back to the live page</a></div>
<main>
__BODY__
</main>
</body>
</html>
"""


def report_html(md_text: str) -> str:
    try:
        import markdown
        body = markdown.markdown(md_text, extensions=["tables"])
    except ImportError:  # still readable without the package
        body = "<pre>" + html.escape(md_text) + "</pre>"
    body = re.sub(r"<table>", '<div class="tablebox"><table>', body)
    body = body.replace("</table>", "</table></div>")
    body = re.sub(r'<img alt="([^"]*)" src="([^"]+)"\s*/?>', r'<img alt="\1" src="\2" loading="lazy">', body)
    return REPORT_PAGE.replace("__BODY__", body)


def build(cfg: Settings) -> Path:
    data = collect(cfg)
    site = cfg.path("site")
    (site / "data.json").write_text(json.dumps(data, default=str))
    page = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", json.dumps(data, default=str).replace("</", "<\\/"))
    out = site / "index.html"
    out.write_text(page, encoding="utf-8")
    vend = site / "vendor"
    vend.mkdir(exist_ok=True)
    for f in VENDOR.glob("*"):
        shutil.copy2(f, vend / f.name)
    fig = cfg.path("reports") / "figures"
    if fig.exists():
        dst = site / "figures"
        dst.mkdir(exist_ok=True)
        for f in fig.glob("*.png"):
            shutil.copy2(f, dst / f.name)
    report = cfg.path("reports") / "REPORT.md"
    if report.exists():
        md = report.read_text(encoding="utf-8")
        shutil.copy2(report, site / "REPORT.md")
        (site / "report.html").write_text(report_html(md), encoding="utf-8")
    else:
        (site / "report.html").write_text(report_html("# Report\n\nThe full analysis has not been run yet."), encoding="utf-8")
    (site / ".nojekyll").write_text("")
    log.info("Web page written to %s", out)
    return out
