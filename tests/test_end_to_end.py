"""The whole project, run against a local fake internet with a PLANTED saving.

If the method works, the estimated saving range for each winter must contain
the saving we planted. This is the strongest test that the analysis is right.
Takes a few minutes, so it is marked "slow".
"""
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from desave.demo import run as demo
from desave.demo import synthetic


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory):
    os.environ["DESAVE_HTTP_PAUSE"] = "0"
    folder = tmp_path_factory.mktemp("demo")
    cfg, srv = demo.setup(folder, quick=True)
    (cfg.home / "README.md").write_text("x\n<!-- HEADLINE:START -->\nold\n<!-- HEADLINE:END -->\ny\n")
    from desave.cli import cmd_all, cmd_check_sources
    assert cmd_check_sources(cfg) == 0
    cmd_all(cfg, backfill_days=10)
    yield cfg
    srv.shutdown()


@pytest.mark.slow
def test_planted_saving_is_found(demo_run):
    cfg = demo_run
    res = json.loads((cfg.path("results") / "savings.json").read_text())
    mock = Path(cfg["demo"]["mock_folder"])
    for row in res["winters"]:
        truth = synthetic.true_winter_saving(mock, cfg, row["winter"])
        assert row["low_pct"] <= truth <= row["high_pct"], (row["winter"], truth, row)
    head = res["headline"]
    assert abs(head["saving_pct"] - synthetic.true_winter_saving(mock, cfg, "2022/23")) < 1.5


@pytest.mark.slow
def test_normal_winters_show_no_saving(demo_run):
    res = json.loads((demo_run.path("results") / "savings.json").read_text())
    for r in res["check_winters"]:
        assert r["low_pct"] <= 0 <= r["high_pct"], r


@pytest.mark.slow
def test_outputs_exist(demo_run):
    cfg = demo_run
    assert (cfg.path("site") / "index.html").read_text().count("GW") > 0
    data = json.loads((cfg.path("site") / "data.json").read_text())
    assert data["tomorrow"]["total_gwh"] > 0 and data["savings"]["headline"]
    assert data["scores"]["model_mape"] < data["scores"]["baseline_mape"]
    assert (cfg.path("reports") / "REPORT.md").exists()
    assert len(list((cfg.path("reports") / "figures").glob("*.png"))) >= 10
    readme = (cfg.home / "README.md").read_text()
    assert "Germany used" in readme and "old" not in readme
    assert (cfg.path("models") / "operational" / "current" / "meta.json").exists()


@pytest.mark.slow
def test_forecasts_are_frozen_and_scored(demo_run):
    cfg = demo_run
    from desave.pipeline import daily
    fc = daily.all_forecasts(cfg)
    assert fc.groupby("target_date").size().between(23, 25).all()
    sc = pd.read_csv(cfg.path("results") / "scores.csv")
    assert len(sc) >= 9 and sc["hours"].between(23, 25).all()
    issued = pd.to_datetime(fc["issued_utc"], utc=True)
    assert (issued < fc.index).all()        # every forecast was made before the hour it describes


@pytest.mark.slow
def test_energy_charts_fills_missing_hours(demo_run):
    """Delete two recent SMARD hours; the backup must put scaled values back."""
    cfg = demo_run
    from desave import store
    from desave.sources import energy_charts
    df = store.load(cfg, "load")
    last_month = sorted((cfg.path("data") / "load").glob("*.parquet"))[-1]
    month = pd.read_parquet(last_month)
    gone = month.index[-30:-28]
    month.drop(gone).to_parquet(last_month)
    assert energy_charts.fill_gaps(cfg) == 2
    back = store.load(cfg, "load")
    assert set(back.loc[gone, "source"]) == {"energy_charts_scaled"}
    ratio = (back.loc[gone, "load_mwh"] / df.loc[gone, "load_mwh"]).to_numpy()
    assert abs(ratio - 1).max() < 0.02   # scaled back to SMARD level
