"""Check our raw yearly totals against the Bundesnetzagentur's published figures.

Same data source (SMARD grid load), so before any weather adjustment our
yearly totals should be within about 1% of the official ones (SMARD revises
values after publication). A bigger difference would mean something in our
download or cleaning is wrong.
"""
from __future__ import annotations

import pandas as pd

from ..config import Settings
from ..timeutils import local_index


def yearly_totals_twh(load: pd.Series, tz: str) -> pd.Series:
    years = local_index(load.index, tz).year
    tot = load.groupby(years).sum() / 1e6
    hours = load.groupby(years).count()
    full = pd.Series({y: (pd.Timestamp(f"{y + 1}-01-01") - pd.Timestamp(f"{y}-01-01")) / pd.Timedelta(hours=1)
                      for y in tot.index})
    return tot[hours >= 0.99 * full]   # complete years only


def check(cfg: Settings, load: pd.Series) -> dict:
    o = cfg.get("official") or {}
    if not o.get("years"):
        return {"rows": []}
    ours = yearly_totals_twh(load, cfg.tz)
    rows = []
    for item in o["years"]:
        y = int(item["year"])
        ours_twh = float(ours[y]) if y in ours.index else None
        ours_prev = float(ours[y - 1]) if (y - 1) in ours.index else None
        rows.append({
            "year": y, "official_twh": item.get("twh"), "official_change_pct": item.get("change_pct"),
            "ours_twh": None if ours_twh is None else round(ours_twh, 1),
            "ours_change_pct": None if (ours_twh is None or ours_prev is None) else round(100 * (ours_twh / ours_prev - 1), 2),
            "diff_pct": None if (ours_twh is None or not item.get("twh")) else round(100 * (ours_twh / item["twh"] - 1), 2),
            "source": item.get("source"),
        })
    srcs = o.get("sources", {})
    used = []
    for r in rows:
        s = srcs.get(r["source"])
        if s and s not in used:
            used.append(s)
    diffs = [abs(r["diff_pct"]) for r in rows if r["diff_pct"] is not None]
    return {"rows": rows, "note": o.get("note", ""), "sources": used,
            "max_abs_diff_pct": max(diffs) if diffs else None,
            "matches": bool(diffs) and max(diffs) < 1.5}
