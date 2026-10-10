"""A grid region with nothing yet is filled in by the daily job itself (the live page once showed empty regions)."""
import pandas as pd

from desave import areas
from desave.analysis import savings
from desave.pipeline import daily


def _spy(monkeypatch):
    calls = []
    monkeypatch.setattr(daily, "backfill", lambda cfg, days=30: calls.append(("backfill", cfg.area)))
    monkeypatch.setattr(savings, "prepare", lambda cfg: None)
    monkeypatch.setattr(savings, "run", lambda cfg, inputs, light=False: calls.append(("savings", cfg.area, light)))
    return calls


def test_empty_region_is_repaired(tmp_cfg, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = areas.scoped(tmp_cfg, "TenneT")
    assert daily.heal(cfg) == ["backfill", "savings"]
    assert calls == [("backfill", "TenneT"), ("savings", "TenneT", True)]


def test_complete_region_is_left_alone(tmp_cfg, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = areas.scoped(tmp_cfg, "Amprion")
    (cfg.path("results") / "savings.json").write_text("{}")
    d = daily.forecasts_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"forecast_mwh": [1.0]}, index=pd.DatetimeIndex(["2024-01-01"], tz="UTC")).to_parquet(d / "2024-01-01.parquet")
    assert daily.heal(cfg) == [] and calls == []


def test_repairs_stop_when_time_runs_out(tmp_cfg, monkeypatch):
    calls = _spy(monkeypatch)
    cfg = areas.scoped(tmp_cfg, "50Hertz")
    long_ago = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=3)
    assert daily.heal(cfg, started=long_ago) == [] and calls == []
