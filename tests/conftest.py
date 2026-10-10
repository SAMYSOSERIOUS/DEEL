from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from desave.config import Settings, load_settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def cfg() -> Settings:
    return load_settings(ROOT)


@pytest.fixture()
def tmp_cfg(tmp_path, cfg) -> Settings:
    """Same settings, but data/models/results go to a temporary folder."""
    return Settings(dict(cfg.raw), tmp_path)


@pytest.fixture(scope="session")
def real_load(cfg):
    """The real downloaded demand, if present (tests using it are skipped otherwise)."""
    from desave import store
    df = store.load(cfg, "load")
    if df.empty or len(df) < 24 * 400:
        pytest.skip("real data not downloaded yet (run: desave download)")
    return df["load_mwh"].dropna()


def hourly(start_local: str, end_local: str, value: float = 50_000.0, tz: str = "Europe/Berlin") -> pd.Series:
    from desave.timeutils import hourly_utc_range
    idx = hourly_utc_range(start_local, end_local, tz)
    return pd.Series(np.full(len(idx), value), index=idx)
