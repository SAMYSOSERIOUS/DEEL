"""Run the whole analysis (steps 2-8) and write reports/REPORT.md.

Every number in the report and in the README headline comes from
results/savings.json. Nothing is typed by hand.
"""
from __future__ import annotations

import json
import logging
import re

import pandas as pd

from .. import validate
from ..config import Settings
from ..sources import eurostat, opsd
from . import plots, savings

log = logging.getLogger(__name__)

LIMITS = """\
**What this analysis can and can't tell apart**

1. **Grid demand is not total use.** SMARD measures electricity flowing through the public grid.
   Power that rooftop solar panels make and use on the spot never shows up. Rooftop solar grew fast
   after 2022, so part of the "saving" in daytime hours may just be self-made solar power. The
   *dark hours only* view removes this: at night no panel produces anything.
2. **Industry or households?** One national number per hour can't say who saved. Two hints only:
   *when* the saving happened (workdays and daytime point to industry and offices; evenings and
   weekends point to homes), and whether energy-hungry industries produced less at the same time
   (Eurostat). A match in timing is a hint, not proof.
3. **Saving or cutting back?** A factory that stops making ammonia "saves" electricity, but that
   is lost production, not efficiency. The data can't tell a smarter fridge from a closed plant.
4. **Prices or appeals?** Prices jumped and the government asked people to save at the same time.
   Both pushed the same way in the same months, so their effects can't be separated here.
5. **New demand hides savings.** Heat pumps and electric cars added demand after 2022. The
   estimate is the *net* change: real savings could be larger than shown.
6. **The further ahead, the less sure.** The "no crisis" model learned from 2015 to August 2022.
   Every winter after that is forecast further into the future, so the ranges get wider. The model
   holds the last pre-crisis year's level; it can't know about slow changes after that.
7. **Starting date matters a little.** Prices already rose from autumn 2021. Training up to
   September 2022 may already include some early crisis behaviour, which would make the saving
   look smaller. The *cutoff* views show how much this changes the answer.
"""


def _pct(x: float) -> str:
    return f"{x:.1f}%"


def _table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
    return "\n".join(lines)


def headline_text(res: dict) -> str:
    h = res.get("headline")
    if not h:
        return "Germany's electricity savings in winter 2022/23: not enough data yet."
    return (f"Germany used {h['saving_pct']:.1f}% less grid electricity in winter 2022/23 than the weather "
            f"predicted (90% range {h['low_pct']:.1f}% to {h['high_pct']:.1f}%).")


def write_report(cfg: Settings, res: dict, checks: dict) -> str:
    w = pd.DataFrame(res["winters"])
    bt = pd.DataFrame(res["backtests"])
    dec = pd.DataFrame(res["decomposition"])
    out = []
    out.append("# Did Germany really save electricity in the 2022 energy crisis?\n")
    out.append(f"*Generated {res['generated_utc']} from data up to {res['data']['last_hour_utc']}.*\n")
    out.append(f"## The short answer\n\n**{headline_text(res)}**\n\n{res['verdict']}\n")

    out.append("## Saving per winter (October to March)\n")
    t = pd.DataFrame({
        "Winter": w["winter"] + w["complete"].map({True: "", False: " (so far)"}),
        "Real use (TWh)": w["actual_twh"].round(1),
        "Expected without crisis (TWh)": w["expected_twh"].round(1),
        "Saving (TWh)": w["saving_twh"].round(1),
        "Saving": w["saving_pct"].map(_pct),
        "90% range": [f"{a:.1f}% to {b:.1f}%" for a, b in zip(w["low_pct"], w["high_pct"])],
        "Years past training": w["years_ahead"],
    })
    out.append(_table(t) + "\n")
    out.append("![Savings by winter](figures/11_savings_by_winter.png)\n")
    out.append("![Winter 2022/23 day by day](figures/12_winter_2022_23.png)\n")

    out.append("## Step 4: does the method work? Tests on normal winters\n")
    out.append("Each normal winter was forecast using only older data, as if it were unknown, from the same "
               "distances ahead as the crisis winters. In a normal winter the 'saving' should be about 0.\n")
    tb = bt[["winter", "gap_years", "error_pct", "daily_mape", "hourly_mape", "baseline_last_year_hourly_mape"]].copy()
    tb.columns = ["Test winter", "Years ahead", "Whole-winter error", "Daily error", "Hourly error",
                  "Hourly error of 'same week last year'"]
    for c in tb.columns[2:]:
        tb[c] = tb[c].map(_pct)
    out.append(_table(tb) + "\n")
    em = res["error_model"]
    out.append(f"Typical whole-winter error: about {100 * (em['a'] ** 0.5):.1f}% for the next winter, growing with "
               f"each year ahead. On average the model was off by {100 * em['drift_intercept']:+.1f}% "
               f"plus {100 * em['drift_per_year']:+.1f}% per year ahead (mostly because demand was slowly "
               f"falling before 2022). The ranges above are built from these errors.\n")
    out.append("![Test on a normal winter](figures/10_test_normal_winter.png)\n")

    out.append("## Why the weather adjustment matters\n")
    d = dec.copy()
    d.columns = ["Winter", "Raw change vs. 2016-2020 winters", "Explained by weather & calendar", "Saving",
                 "Left over"]
    for c in d.columns[1:]:
        d[c] = d[c].map(_pct)
    out.append(_table(d) + "\n")
    simple = res["variants"].get("simple average of 2017-19 (no weather adjustment)", [])
    if simple:
        s = next((r for r in simple if r["winter"] == "2022/23"), None)
        if s:
            out.append(f"Comparing with a plain 2017-2019 average (no weather adjustment) would give "
                       f"{s['saving_pct']:.1f}% for 2022/23, instead of {res['headline']['saving_pct']:.1f}%.\n")

    out.append("## Does the answer depend on our choices?\n")
    rows = []
    for name, v in res["variants"].items():
        for r in v:
            if r["winter"] in ("2022/23", (res.get("latest_complete_winter") or {}).get("winter")):
                rows.append({"View": name, "Winter": r["winter"], "Saving": _pct(r["saving_pct"]),
                             "90% range": f"{r['low_pct']:.1f}% to {r['high_pct']:.1f}%"})
    out.append(_table(pd.DataFrame(rows)) + "\n")
    out.append("![Robustness](figures/15_robustness.png)\n")

    out.append("## When did the saving happen, and who might have saved?\n")
    p = res.get("profile_2022_23") or {}
    if p:
        out.append(f"In winter 2022/23 the saving was {_pct(p['workdays_pct'])} on workdays and "
                   f"{_pct(p['weekends_holidays_pct'])} on weekends and holidays "
                   f"({_pct(p['workday_daytime_pct'])} in workday daytime, 7:00-18:00).\n")
        out.append("![Saving by hour](figures/14_saving_by_hour.png)\n")
    ind = res.get("industry", {})
    if ind.get("available"):
        out.append(f"Energy-hungry industries ({', '.join(eurostat.NAMES[c] for c in ind['series'])}) produced "
                   f"{ind['industry_change_first_crisis_winter_pct']:+.1f}% compared with 2021 during winter 2022/23. "
                   f"Month by month, the saving and the drop in industrial output move together with a "
                   f"correlation of {ind['correlation_saving_vs_industry_drop']:.2f} "
                   f"(1 = in perfect step, 0 = unrelated) over {ind['months']} months.\n")
    out.append("![Monthly saving and industry](figures/13_monthly_and_industry.png)\n")
    out.append(LIMITS + "\n")

    out.append("## Data checks\n")
    lc = checks.get("load", {})
    out.append(f"- Demand data: {lc.get('hours', '?')} hours from {lc.get('first', '?')} to {lc.get('last', '?')}; "
               f"{lc.get('days_23h', '?')} 23-hour days and {lc.get('days_25h', '?')} 25-hour days (clock changes); "
               f"{lc.get('gaps_total_hours', 0)} missing hours filled (no gap longer than 2 hours allowed).")
    oc = checks.get("opsd", {})
    if oc.get("overlap_hours"):
        out.append(f"- Cross-check with Open Power System Data ({oc['from'][:10]} to {oc['to'][:10]}): daily "
                   f"correlation {oc['daily_correlation']}, SMARD is on average {oc['median_ratio_smard_to_opsd']:.3f}× "
                   f"OPSD (different definition of 'load', steady over the years: "
                   + ", ".join(f"{y}: {v}" for y, v in oc["yearly_ratio"].items()) + ").")
    out.append("\n## Exploring the data (step 2)\n")
    for f, cap in (("01_history", "History"), ("02_daily_rhythm", "Daily rhythm"), ("03_temperature", "Temperature"),
                   ("04_christmas", "Christmas"), ("05_lockdown", "Lockdown"), ("06_clock_change", "Clock change")):
        out.append(f"![{cap}](figures/{f}.png)\n")
    out.append("\n## Sources\n- Electricity: Bundesnetzagentur | SMARD.de (CC BY 4.0)\n"
               "- Weather: Deutscher Wetterdienst (DWD), opendata.dwd.de\n"
               "- Cross-check: Open Power System Data, time series package 2020-10-06\n"
               "- Industry: Eurostat, sts_inpr_m\n- Holidays: python-holidays\n")
    text = "\n".join(out)
    (cfg.path("reports") / "REPORT.md").write_text(text, encoding="utf-8")
    return text


def update_readme_headline(cfg: Settings, res: dict) -> bool:
    f = cfg.home / "README.md"
    if not f.exists():
        return False
    txt = f.read_text(encoding="utf-8")
    block = (f"<!-- HEADLINE:START -->\n## {headline_text(res)}\n\n{res['verdict']}\n\n"
             f"*Updated automatically on {res['generated_utc'][:10]}. Full report: "
             f"[reports/REPORT.md](reports/REPORT.md)*\n<!-- HEADLINE:END -->")
    new = re.sub(r"<!-- HEADLINE:START -->.*?<!-- HEADLINE:END -->", block, txt, flags=re.S)
    if new != txt:
        f.write_text(new, encoding="utf-8")
    return True


def full(cfg: Settings) -> dict:
    inp = savings.prepare(cfg)
    checks = {"load": validate.check_load(inp.load, cfg.tz).stats}
    try:
        checks["opsd"] = opsd.cross_check(inp.load, opsd.download(cfg))
    except Exception as e:  # noqa: BLE001
        log.warning("OPSD cross-check skipped: %s", e)
    try:
        industry = eurostat.download_all(cfg)
    except Exception as e:  # noqa: BLE001
        log.warning("Eurostat skipped: %s", e)
        industry = None
    plots.explore(cfg, inp.load, inp.X)
    res = savings.run(cfg, inp, industry=industry)
    res["checks"] = checks
    (cfg.path("results") / "savings.json").write_text(json.dumps(res, indent=2, default=savings._json_default))
    plots.results(cfg, res, inp.load)
    write_report(cfg, res, checks)
    update_readme_headline(cfg, res)
    return res
