"""Open Power System Data: cleaned German load 2015 to mid-2020, used as a cross-check.

OPSD's German load comes from ENTSO-E, which defines "load" a bit differently
from SMARD. So we don't expect identical numbers, only the same SHAPE
(same daily ups and downs). The cross-check reports the correlation and the
typical ratio between the two.
"""
from __future__ import annotations

import io
import logging

import numpy as np
import pandas as pd

from ..config import Settings
from ..http import get

log = logging.getLogger(__name__)


def parse_csv(text_or_buffer, column: str) -> pd.Series:
    df = pd.read_csv(text_or_buffer, usecols=["utc_timestamp", column])
    t = pd.to_datetime(df["utc_timestamp"], utc=True)
    return pd.Series(df[column].to_numpy(dtype="float64"), index=pd.DatetimeIndex(t), name="opsd").dropna()


def download(cfg: Settings) -> pd.Series:
    f = cfg.path("data") / "opsd_de_load.parquet"
    if f.exists():
        return pd.read_parquet(f)["opsd"]
    s = cfg["sources"]["opsd"]
    r = get(s["url"], timeout=600)
    series = parse_csv(io.StringIO(r.text), s["column"])
    series.to_frame().to_parquet(f)
    return series


def cross_check(smard: pd.Series, opsd: pd.Series) -> dict:
    both = pd.concat([smard.rename("smard"), opsd.rename("opsd")], axis=1).dropna()
    if both.empty:
        return {"overlap_hours": 0}
    ratio = both["smard"] / both["opsd"]
    daily = both.resample("D").sum()
    daily = daily[(both.resample("D").count()["smard"] >= 23)]
    return {
        "overlap_hours": int(len(both)),
        "from": str(both.index.min()), "to": str(both.index.max()),
        "hourly_correlation": round(float(both.corr().iloc[0, 1]), 4),
        "daily_correlation": round(float(daily.corr().iloc[0, 1]), 4),
        "median_ratio_smard_to_opsd": round(float(ratio.median()), 4),
        "ratio_p05": round(float(ratio.quantile(0.05)), 4),
        "ratio_p95": round(float(ratio.quantile(0.95)), 4),
        "yearly_ratio": {int(y): round(float(v), 4) for y, v in
                         (both["smard"].groupby(both.index.year).sum() /
                          both["opsd"].groupby(both.index.year).sum()).items()},
        "hours_differing_more_than_10pct": int((np.abs(ratio / ratio.median() - 1) > 0.10).sum()),
    }
