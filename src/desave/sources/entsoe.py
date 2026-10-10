"""ENTSO-E Transparency Platform: backup source for German load.

Needs a free API key (register on transparency.entsoe.eu, then e-mail
transparency@entsoe.eu asking for "Restful API access"). Put it in the
environment variable named in settings (default ``ENTSOE_API_KEY``).

ENTSO-E "actual total load" is defined slightly differently from SMARD's grid
load, so before filling SMARD gaps we scale ENTSO-E to SMARD using the hours
where both exist (see :func:`calibrate`).
"""
from __future__ import annotations

import logging
import os
import re
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd

from .. import store
from ..config import Settings
from ..http import get

log = logging.getLogger(__name__)

_RES = {"PT15M": pd.Timedelta(minutes=15), "PT30M": pd.Timedelta(minutes=30), "PT60M": pd.Timedelta(hours=1)}


def _strip_ns(tag: str) -> str:
    return re.sub(r"^\{.*?\}", "", tag)


def parse_load_xml(xml_text: str) -> pd.Series:
    """Parse an ENTSO-E GL_MarketDocument (document type A65) into hourly MWh (UTC).

    Handles 15-minute and hourly resolution, and the "A03" curve type where
    repeated values are left out (a missing position means "same as before").
    """
    root = ET.fromstring(xml_text)
    out = []
    for ts in root.iter():
        if _strip_ns(ts.tag) != "TimeSeries":
            continue
        curve = next((c.text for c in ts.iter() if _strip_ns(c.tag) == "curveType"), "A01")
        for period in (p for p in ts.iter() if _strip_ns(p.tag) == "Period"):
            start = end = res = None
            pts = {}
            for el in period.iter():
                t = _strip_ns(el.tag)
                if t == "start":
                    start = pd.Timestamp(el.text)
                elif t == "end":
                    end = pd.Timestamp(el.text)
                elif t == "resolution":
                    res = _RES[el.text]
                elif t == "Point":
                    pos = qty = None
                    for c in el:
                        ct = _strip_ns(c.tag)
                        if ct == "position":
                            pos = int(c.text)
                        elif ct == "quantity":
                            qty = float(c.text)
                    pts[pos] = qty
            n = int((end - start) / res)
            vals = np.full(n, np.nan)
            for p, q in pts.items():
                if 1 <= p <= n:
                    vals[p - 1] = q
            if curve == "A03":
                vals = pd.Series(vals).ffill().to_numpy()
            idx = pd.date_range(start, periods=n, freq=res)
            if idx.tz is None:
                idx = idx.tz_localize("UTC")
            out.append(pd.Series(vals, index=idx.tz_convert("UTC")))
    if not out:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.concat(out).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    # MW averaged over each hour = MWh in that hour
    return s.resample("h").mean().dropna()


def download(cfg: Settings, start: pd.Timestamp, end: pd.Timestamp) -> pd.Series:
    s = cfg["sources"]["entsoe"]
    token = os.environ.get(s["token_env"])
    if not token:
        raise RuntimeError(f"ENTSO-E backup needs an API key in ${s['token_env']}")
    parts = []
    cur = start
    while cur < end:  # the API allows at most one year per request
        nxt = min(cur + pd.Timedelta(days=360), end)
        params = {
            "securityToken": token, "documentType": "A65", "processType": "A16",
            "outBiddingZone_Domain": s["area"],
            "periodStart": cur.strftime("%Y%m%d%H00"), "periodEnd": nxt.strftime("%Y%m%d%H00"),
        }
        r = get(s["base_url"], params=params, timeout=120)
        parts.append(parse_load_xml(r.text))
        cur = nxt
    return pd.concat(parts).sort_index() if parts else pd.Series(dtype="float64")


def calibrate(smard: pd.Series, entsoe: pd.Series, days: int = 365) -> pd.Series:
    """Hour-of-day scale factors that turn ENTSO-E load into SMARD-like load."""
    both = pd.concat([smard.rename("s"), entsoe.rename("e")], axis=1).dropna()
    both = both[both.index >= both.index.max() - pd.Timedelta(days=days)]
    if len(both) < 24 * 14:
        raise RuntimeError("Not enough overlap between SMARD and ENTSO-E to calibrate")
    ratio = (both["s"] / both["e"]).groupby(both.index.hour).median()
    return ratio


def fill_gaps(cfg: Settings, lookback_days: int = 30) -> int:
    """Fill missing recent SMARD hours with calibrated ENTSO-E values. Returns hours filled."""
    now = pd.Timestamp.now(tz="UTC").floor("h")
    hist = store.load(cfg, "load", start=now - pd.Timedelta(days=400))
    if hist.empty:
        return 0
    full_idx = pd.date_range(now - pd.Timedelta(days=lookback_days), now - pd.Timedelta(days=1), freq="h")
    missing = full_idx.difference(hist.index[hist["load_mwh"].notna()])
    if len(missing) == 0:
        return 0
    ent = download(cfg, hist.index.min(), now)
    ratio = calibrate(hist["load_mwh"], ent)
    fill = ent.reindex(missing).dropna()
    fill = fill * ratio.reindex(fill.index.hour).to_numpy()
    df = fill.to_frame("load_mwh")
    df["source"] = "entsoe_calibrated"
    store.save_monthly(cfg, "load", df)
    log.warning("Filled %d missing SMARD hours with calibrated ENTSO-E data", len(df))
    return len(df)
