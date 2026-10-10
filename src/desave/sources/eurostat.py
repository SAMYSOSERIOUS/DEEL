"""Eurostat monthly industrial production index for Germany (table sts_inpr_m).

Used only for the "what else could explain the gap" discussion: did
energy-hungry industries (paper, chemicals, glass/cement, metals) produce less
at the same time demand fell? Seasonally and calendar adjusted, 2021 = 100.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from ..config import Settings
from ..http import get

log = logging.getLogger(__name__)

NAMES = {"C17": "Paper", "C20": "Chemicals", "C23": "Glass, cement, ceramics",
         "C24": "Basic metals", "B-D": "All industry"}


def parse_jsonstat(payload: dict) -> pd.Series:
    """Read a one-series JSON-stat answer into a monthly series."""
    dims = payload["id"]
    sizes = payload["size"]
    time_dim = "time"
    t_index = payload["dimension"][time_dim]["category"]["index"]
    pos_to_time = {v: k for k, v in t_index.items()}
    stride = int(np.prod(sizes[dims.index(time_dim) + 1:])) if dims.index(time_dim) + 1 < len(dims) else 1
    vals = {}
    for k, v in payload.get("value", {}).items():
        tpos = (int(k) // stride) % sizes[dims.index(time_dim)]
        vals[pd.Period(pos_to_time[tpos], freq="M")] = float(v)
    return pd.Series(vals).sort_index()


def download(cfg: Settings, nace: str, since: str = "2015-01") -> pd.Series:
    url = f"{cfg['sources']['eurostat']['base_url']}/sts_inpr_m"
    params = {"geo": "DE", "nace_r2": nace, "s_adj": "SCA", "unit": "I21", "format": "JSON",
              "lang": "EN", "sinceTimePeriod": since}
    r = get(url, params=params)
    return parse_jsonstat(r.json()).rename(nace)


def download_all(cfg: Settings) -> pd.DataFrame:
    f = cfg.path("data") / "industry_production.parquet"
    codes = list(cfg["analysis"]["industry_series"]) + ["B-D"]
    cols = {}
    for c in codes:
        try:
            cols[c] = download(cfg, c)
        except Exception as e:  # noqa: BLE001
            log.warning("Eurostat %s failed: %s", c, e)
    if not cols:
        if f.exists():
            return pd.read_parquet(f)
        return pd.DataFrame()
    df = pd.DataFrame(cols)
    df.index = df.index.astype(str)
    df.to_parquet(f)
    return df
