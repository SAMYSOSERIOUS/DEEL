"""Charts for exploring the data (step 2) and showing the results (steps 4-8).

Colors are fixed by role, never by position:
  blue   = what really happened
  orange = "no crisis" forecast (the model)
  aqua   = simple baseline
Grey shading marks the crisis period or the 90% range.
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ..config import Settings  # noqa: E402
from ..timeutils import local_dates, local_index, winter_bounds  # noqa: E402

ACTUAL, MODEL, BASE = "#2a78d6", "#eb6834", "#1baf7a"
INK, INK2, GRID, SHADE = "#0b0b0b", "#52514e", "#e6e5e0", "#d9d8d3"

plt.rcParams.update({
    "figure.dpi": 110, "savefig.dpi": 140, "font.size": 10, "axes.edgecolor": INK2,
    "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2, "axes.titleweight": "bold",
    "axes.titlesize": 12, "axes.titlecolor": INK, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "lines.linewidth": 1.6,
    "legend.frameon": False, "figure.facecolor": "white", "axes.facecolor": "white",
})


def _save(fig, out: Path, name: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{name}.png"
    fig.tight_layout()
    fig.savefig(p, bbox_inches="tight")
    plt.close(fig)
    return p


def _daily(series: pd.Series, tz: str, how: str = "mean") -> pd.Series:
    d = series.groupby(local_dates(series.index, tz)).agg(how)
    d.index = pd.to_datetime(d.index)
    return d


# ------------------------------------------------------------- step 2: explore
def explore(cfg: Settings, load: pd.Series, X: pd.DataFrame) -> list[Path]:
    out = cfg.path("reports") / "figures"
    tz = cfg.tz
    files = []
    gw = load / 1000  # MWh per hour = MW; /1000 = GW

    # 1. the whole history
    d = _daily(gw, tz)
    fig, ax = plt.subplots(figsize=(11, 4))
    ax.plot(d.index, d, color=ACTUAL, lw=0.5, alpha=0.35, label="daily average")
    ax.plot(d.index, d.rolling(28, center=True).mean(), color=ACTUAL, lw=2, label="4-week average")
    ax.axvspan(pd.Timestamp("2020-03-16"), pd.Timestamp("2020-06-30"), color=SHADE, alpha=0.6, lw=0)
    ax.text(pd.Timestamp("2020-05-01"), d.max(), "lockdown", ha="center", va="top", color=INK2, fontsize=9)
    ax.axvspan(pd.Timestamp("2022-09-01"), pd.Timestamp("2023-04-01"), color=SHADE, alpha=0.6, lw=0)
    ax.text(pd.Timestamp("2022-12-15"), d.max(), "crisis winter", ha="center", va="top", color=INK2, fontsize=9)
    ax.set(title="German electricity use, 2015 to today", ylabel="average demand (GW)")
    ax.legend(loc="lower left")
    files.append(_save(fig, out, "01_history"))

    # 2. daily rhythm
    loc = local_index(gw.index, tz)
    winter = np.isin(loc.month, [12, 1, 2])
    summer = np.isin(loc.month, [6, 7, 8])
    pre = loc.year < 2022
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8), sharey=True)
    for ax, sel, name in ((axes[0], winter & pre, "Winter (Dec-Feb)"), (axes[1], summer & pre, "Summer (Jun-Aug)")):
        for label, mask, color, ls in (("Workday", loc.dayofweek < 5, ACTUAL, "-"),
                                       ("Saturday", loc.dayofweek == 5, MODEL, "-"),
                                       ("Sunday", loc.dayofweek == 6, BASE, "-")):
            m = sel & mask & (X["holiday_share"].reindex(gw.index).to_numpy() < 0.5)
            prof = gw[m].groupby(loc.hour[m]).mean()
            ax.plot(prof.index, prof, color=color, ls=ls, label=label, marker="o", ms=3)
        ax.set(title=name, xlabel="hour of day (German time)", xticks=range(0, 24, 3))
    axes[0].set_ylabel("average demand (GW)")
    axes[0].legend()
    fig.suptitle("The daily rhythm: low at night, high on workdays, lower in summer (2015-2021)",
                 x=0.01, ha="left", fontweight="bold")
    files.append(_save(fig, out, "02_daily_rhythm"))

    # 3. temperature
    dt = _daily(X["temp"].reindex(gw.index), tz)
    dl = d.reindex(dt.index)
    wd = pd.Series(pd.to_datetime(dt.index).dayofweek < 5, index=dt.index)
    hol = _daily(X["holiday_share"].reindex(gw.index), tz) < 0.5
    fig, ax = plt.subplots(figsize=(7.5, 5))
    for label, m, color in (("2015-2021", (dt.index < "2022-01-01"), ACTUAL),
                            ("since Sept 2022", (dt.index >= "2022-09-01"), MODEL)):
        mm = m & wd.to_numpy() & hol.reindex(dt.index).fillna(False).to_numpy()
        ax.scatter(dt[mm], dl[mm], s=6, alpha=0.35, color=color, label=label, lw=0)
    ax.set(title="Colder days use more electricity (workdays only)", xlabel="daily average temperature (°C)",
           ylabel="average demand (GW)")
    ax.legend(markerscale=3)
    files.append(_save(fig, out, "03_temperature"))

    # 4. holidays: Christmas to New Year
    fig, ax = plt.subplots(figsize=(11, 4))
    years = sorted({t.year for t in d.index if t.month == 12})
    for i, y in enumerate(years[-5:]):
        rng_ = pd.date_range(f"{y}-12-10", f"{y + 1}-01-15")
        seg = d.reindex(rng_)
        x = (rng_ - pd.Timestamp(f"{y}-12-25")).days
        ax.plot(x, seg, lw=1.4, color=[ACTUAL, MODEL, BASE, "#eda100", "#6250d6"][i % 5], label=f"{y}/{(y + 1) % 100:02d}")
    ax.axvline(0, color=INK2, lw=0.8, ls=":")
    ax.text(0.3, ax.get_ylim()[1], "25 Dec", va="top", color=INK2, fontsize=9)
    ax.set(title="Holidays: demand drops sharply over Christmas and New Year", xlabel="days from 25 December",
           ylabel="average demand (GW)")
    ax.legend(ncols=5, loc="lower left")
    files.append(_save(fig, out, "04_christmas"))

    # 5. lockdown
    fig, ax = plt.subplots(figsize=(11, 4))
    span = pd.date_range("2020-02-01", "2020-07-31")
    ref = []
    for k in (1, 2, 3):
        ref.append(d.reindex(span - pd.Timedelta(days=364 * k)).to_numpy())
    ref = pd.Series(np.nanmean(np.vstack(ref), axis=0), index=span)
    ax.plot(span, ref.rolling(7, center=True).mean(), color=BASE, label="same weeks 2017-2019 (7-day avg)")
    ax.plot(span, d.reindex(span).rolling(7, center=True).mean(), color=ACTUAL, label="2020 (7-day avg)")
    ax.axvspan(pd.Timestamp("2020-03-16"), pd.Timestamp("2020-05-10"), color=SHADE, alpha=0.6, lw=0)
    ax.set(title="The 2020 lockdown cut demand for about two months", ylabel="average demand (GW)")
    ax.legend()
    files.append(_save(fig, out, "05_lockdown"))

    # 6. clock change check
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.5))
    for ax, month in zip(axes, (3, 10)):
        last = local_index(gw.index, tz)[-1]
        y = last.year if (last.month, last.day) > (month, 31) or last.month > month else last.year - 1
        last_sun = [t for t in pd.date_range(f"{y}-{month:02d}-25", periods=7) if t.dayofweek == 6][0]
        s = pd.Timestamp(last_sun - pd.Timedelta(days=1)).tz_localize(tz).tz_convert("UTC")
        e = pd.Timestamp(last_sun + pd.Timedelta(days=2)).tz_localize(tz).tz_convert("UTC")
        seg = gw[(gw.index >= s) & (gw.index < e)]
        ax.plot(seg.index.tz_convert(tz), seg, color=ACTUAL, marker="o", ms=2.5)
        n = len(seg[local_dates(seg.index, tz) == last_sun.date()])
        ax.set(title=f"{last_sun.date()}: {n} hours", ylabel="GW")
        ax.tick_params(axis="x", rotation=30)
    fig.suptitle("Clock changes handled: the March day has 23 hours, the October day 25", x=0.01, ha="left",
                 fontweight="bold")
    files.append(_save(fig, out, "06_clock_change"))
    return files


# ----------------------------------------------------------- steps 4-8: results
def results(cfg: Settings, res: dict, load: pd.Series) -> list[Path]:
    out = cfg.path("reports") / "figures"
    tz = cfg.tz
    files = []
    rdir = cfg.path("results")

    # A. test on normal winters
    bt = pd.read_csv(rdir / "backtest_daily.csv", index_col=0, parse_dates=True)
    if len(bt):
        w = "2018/19" if "2018/19" in set(bt["winter"]) else bt["winter"].iloc[0]
        b = bt[bt["winter"] == w]
        fig, ax = plt.subplots(figsize=(11, 4))
        ax.plot(b.index, b["baseline"] / 24e3, color=BASE, lw=1.2, label="same week last year")
        ax.plot(b.index, b["model"] / 24e3, color=MODEL, label="model forecast")
        ax.plot(b.index, b["actual"] / 24e3, color=ACTUAL, label="what really happened")
        row = next(r for r in res["backtests"] if r["winter"] == w and r["gap_years"] == 0)
        ax.set(title=f"Test on a normal winter ({w}), forecast only from older data: "
                     f"whole-winter error {row['error_pct']:+.1f}%, daily error {row['daily_mape']:.1f}%",
               ylabel="average demand (GW)")
        ax.legend(ncols=3, loc="lower left")
        files.append(_save(fig, out, "10_test_normal_winter"))

    # B. savings by winter with ranges (checks first, then crisis winters)
    rows = [dict(r, kind="check") for r in res["check_winters"]] + \
           [dict(r, kind="crisis") for r in res["winters"]]
    if rows:
        fig, ax = plt.subplots(figsize=(11, 4.5))
        x = np.arange(len(rows))
        for i, r in enumerate(rows):
            color = INK2 if r["kind"] == "check" else MODEL
            ax.bar(i, r["saving_pct"], color=color, width=0.55, alpha=0.9 if r["kind"] == "crisis" else 0.45)
            ax.errorbar(i, r["saving_pct"], yerr=[[r["saving_pct"] - r["low_pct"]], [r["high_pct"] - r["saving_pct"]]],
                        color=INK, capsize=4, lw=1.2)
            ax.text(i, r["high_pct"] + 0.25, f"{r['saving_pct']:.1f}%", ha="center", va="bottom", fontsize=9, color=INK)
        ax.axhline(0, color=INK2, lw=1)
        ax.set_xticks(x, [r["winter"] + ("\n(test)" if r["kind"] == "check" else
                                         ("" if r.get("complete", True) else "\n(so far)")) for r in rows])
        ax.set(title="Saving compared with a normal winter (Oct-Mar), with 90% range.\n"
                     "Grey = normal winters used as a test (should be near 0). Orange = crisis and after.",
               ylabel="less electricity than expected (%)")
        files.append(_save(fig, out, "11_savings_by_winter"))

    # C. the 2022/23 winter day by day
    hourly = pd.read_parquet(rdir / "counterfactual_hourly.parquet")
    ws, we = winter_bounds("2022/23", cfg["analysis"]["winter_months"], tz)
    seg = hourly[(hourly.index >= ws - pd.Timedelta(days=60)) & (hourly.index < we)]
    if len(seg):
        da = _daily(seg["actual_mwh"] / 1e3, tz)
        de = _daily(seg["expected_no_crisis_mwh"] / 1e3, tz)
        # typical error of a single DAY in the tests on normal winters (not the whole-winter error)
        daily_err = [r["daily_mape"] for r in res["backtests"] if r["gap_years"] == 0]
        typical = (np.mean(daily_err) / 100) * 1.25 if daily_err else 0.02  # mean abs error -> std (~1.25x)
        fig, ax = plt.subplots(figsize=(11, 4))
        ax.fill_between(de.index, de * (1 - 1.645 * typical), de * (1 + 1.645 * typical), color=SHADE, alpha=0.6,
                        lw=0, label="90% range for a single day")
        ax.plot(de.index, de, color=MODEL, label="expected without the crisis")
        ax.plot(da.index, da, color=ACTUAL, label="what really happened")
        ax.axvline(pd.Timestamp("2022-09-01"), color=INK2, lw=0.8, ls=":")
        ax.set(title="Winter 2022/23: real demand stayed below what the weather predicted",
               ylabel="average demand (GW)")
        ax.legend(ncols=3, loc="lower left")
        files.append(_save(fig, out, "12_winter_2022_23"))

    # D. monthly savings (+ industry, as separate panel: never two y-axes)
    m = pd.DataFrame(res["monthly"]).set_index("month")
    if len(m):
        ind = res.get("industry", {})
        has_ind = ind.get("available")
        fig, axes = plt.subplots(2 if has_ind else 1, 1, figsize=(11, 6 if has_ind else 3.8), sharex=True,
                                 squeeze=False)
        ax = axes[0, 0]
        xs = pd.PeriodIndex(m.index, freq="M").to_timestamp()
        ax.bar(xs, m["saving_pct"], width=25, color=[MODEL if t >= pd.Timestamp(cfg["analysis"]["main_cutoff"])
                                                     else INK2 for t in xs], alpha=0.85)
        ax.axhline(0, color=INK2, lw=1)
        ax.axvline(pd.Timestamp(cfg["analysis"]["main_cutoff"]), color=INK2, lw=0.8, ls=":")
        ax.set(title="Saving month by month (grey = training period, should be ~0)", ylabel="less than expected (%)")
        if has_ind:
            t = pd.DataFrame(ind["table"]).T
            ax2 = axes[1, 0]
            tx = pd.PeriodIndex(t.index, freq="M").to_timestamp()
            ax2.plot(tx, t["industry_change_pct"], color=BASE, marker="o", ms=3)
            ax2.axhline(0, color=INK2, lw=1)
            ax2.set(title="Production of energy-hungry industries vs. 2021 (paper, chemicals, glass/cement, metals)",
                    ylabel="change vs. 2021 (%)")
        files.append(_save(fig, out, "13_monthly_and_industry"))

    # E. when in the day
    prof = res.get("profile_2022_23") or {}
    if prof.get("by_hour"):
        bh = pd.Series({int(k): v for k, v in prof["by_hour"].items()}).sort_index()
        fig, ax = plt.subplots(figsize=(9, 3.8))
        ax.bar(bh.index, bh.values, color=MODEL, width=0.7)
        ax.axhline(0, color=INK2, lw=1)
        ax.set(title=f"Winter 2022/23: saving by hour of day. Workdays {prof['workdays_pct']:.1f}%, "
                     f"weekends & holidays {prof['weekends_holidays_pct']:.1f}%",
               xlabel="hour of day (German time)", ylabel="less than expected (%)", xticks=range(0, 24, 2))
        files.append(_save(fig, out, "14_saving_by_hour"))

    # F. robustness: same answer under different choices?
    v = res.get("variants", {})
    if v:
        names = list(v.keys())
        winters = [r["winter"] for r in res["winters"] if r.get("complete", True)]
        fig, ax = plt.subplots(figsize=(11, 0.45 * len(names) * max(1, len(winters)) / 2 + 2.5))
        y = 0
        ticks, labels = [], []
        colors = [ACTUAL, MODEL, BASE, "#eda100", "#e87ba4", "#6250d6"]
        for i, w in enumerate(winters):
            for name in names:
                r = next((r for r in v[name] if r["winter"] == w), None)
                if r is None:
                    continue
                ax.errorbar(r["saving_pct"], y, xerr=[[r["saving_pct"] - r["low_pct"]], [r["high_pct"] - r["saving_pct"]]],
                            fmt="o", color=colors[i % len(colors)], ms=5, capsize=3, lw=1.2)
                ticks.append(y)
                labels.append(f"{w}  {name}")
                y += 1
            y += 0.8
        ax.axvline(0, color=INK2, lw=1)
        ax.set_yticks(ticks, labels, fontsize=8)
        ax.invert_yaxis()
        ax.set(title="Does the answer depend on our choices? Each line = one way of doing it",
               xlabel="less electricity than expected (%)")
        files.append(_save(fig, out, "15_robustness"))
    return files


def save_json(obj, path: Path) -> None:
    path.write_text(json.dumps(obj, indent=2, default=str))

