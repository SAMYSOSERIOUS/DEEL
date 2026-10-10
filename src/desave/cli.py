"""Command line: ``desave <command>``.

First time:      desave check-sources   (can we download automatically?)
                 desave all             (download everything, analyse, train, build the page)
Every morning:   desave daily
Once a month:    desave retrain
Try it offline:  desave demo
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import pandas as pd

from .config import load_settings


def _cfg(args):
    return load_settings(args.home) if getattr(args, "home", None) else load_settings()


def cmd_check_sources(cfg) -> int:
    """Step 0: confirm every source can be downloaded and read automatically."""
    from .http import get
    from .sources import dwd, eurostat, smard, weather_forecast
    ok = True
    now = pd.Timestamp.now(tz="UTC").floor("h")

    def report(name, fn):
        nonlocal ok
        try:
            msg = fn()
            print(f"  OK    {name}: {msg}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")

    print("Checking data sources...")
    def need_rows(df_or_s, what):
        if len(df_or_s) == 0:
            raise RuntimeError(f"no {what} returned")
        return df_or_s

    report("SMARD demand", lambda: (lambda s: f"{len(s)} hours, latest {s.index.max()}")(
        need_rows(smard.download(cfg, now - pd.Timedelta(days=10)), "hours")))
    from . import areas
    for code in areas.region_codes(cfg):
        report(f"SMARD region {code}", lambda code=code: (lambda s: f"{len(s)} hours, latest {s.index.max()}")(
            need_rows(smard.download(areas.scoped(cfg, code), now - pd.Timedelta(days=10)), "hours")))
    def dwd_recent():
        st = dwd.station_list(cfg, "temp")
        berlin = next(p for p in cfg["weather_points"] if p["name"] == "Berlin")
        sid = dwd.choose_stations(st, [berlin], cfg["project"]["start"])["Berlin"]["station_id"]
        s = dwd.station_series(cfg, "temp", sid, include_historical=False)
        if s.notna().sum() == 0:
            raise RuntimeError(f"station {sid} returned no data")
        return f"{len(st)} stations listed; station {sid} (Berlin): {s.notna().sum()} recent hours, latest {s.index.max()}"

    report("DWD temperature", dwd_recent)
    report("Weather forecast (Bright Sky / DWD MOSMIX)", lambda: (lambda d: f"{len(need_rows(d, 'hours'))} hours")(
        weather_forecast.parse_brightsky(get(
            f"{cfg['sources']['brightsky']['base_url']}/weather",
            params={"lat": 52.47, "lon": 13.40, "date": now.isoformat(),
                    "last_date": (now + pd.Timedelta(days=2)).isoformat()}).json())))
    report("Eurostat industry index", lambda: f"{len(need_rows(eurostat.download(cfg, 'C20'), 'months'))} months")
    from .sources import energy_charts
    report("Energy-Charts backup (no key)", lambda: f"{len(need_rows(energy_charts.download(cfg, now - pd.Timedelta(days=3), now), 'hours'))} hours")
    key = os.environ.get(cfg["sources"]["entsoe"]["token_env"])
    if key:
        from .sources import entsoe
        report("ENTSO-E backup", lambda: f"{len(entsoe.download(cfg, now - pd.Timedelta(days=3), now))} hours")
    else:
        print(f"  --    ENTSO-E backup: not used (optional; needs a key in ${cfg['sources']['entsoe']['token_env']})")
    print("All sources work." if ok else "Some sources failed (see above).")
    return 0 if ok else 1


def cmd_download(cfg, full: bool) -> None:
    from . import areas
    from .sources import dwd, eurostat, opsd, smard
    smard.update(cfg, full=full)
    for code in areas.region_codes(cfg):
        try:
            smard.update(areas.scoped(cfg, code), full=full)
        except Exception as e:  # noqa: BLE001
            logging.warning("SMARD %s download failed: %s", code, e)
    dwd.update(cfg, full=full)
    eurostat.download_all(cfg)
    try:
        opsd.download(cfg)
    except Exception as e:  # noqa: BLE001
        logging.warning("OPSD download skipped: %s", e)


def cmd_check(cfg) -> int:
    from . import dataset, store, validate
    raw = store.load(cfg, cfg.load_store)["load_mwh"].dropna()
    rep = validate.check_load(raw, cfg.tz)
    w = dataset.weather_observed(cfg)
    rep_w = validate.check_weather(w["temp"].dropna(), w["sun"].dropna())
    print(json.dumps({"demand": rep.__dict__, "weather": rep_w.__dict__}, indent=2, default=str))
    return 0 if rep.ok and rep_w.ok else 1


def cmd_all(cfg, skip_download: bool = False, backfill_days: int = 30) -> None:
    from .analysis import report
    from .pipeline import daily
    from .models import operational
    from . import areas, dataset
    if not skip_download:
        cmd_download(cfg, full=False)
    report.full(cfg)
    for code in areas.measured(cfg):
        c = areas.scoped(cfg, code)
        try:
            operational.train(c, dataset.load_series(c), dataset.weather_observed(c), force=True)
            if backfill_days:
                daily.backfill(c, backfill_days)
        except Exception as e:  # noqa: BLE001
            if code == areas.COUNTRY:
                raise
            logging.warning("Setting up %s failed: %s", code, e)
    daily.run(cfg, skip_download=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="desave", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="project folder (default: current folder)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-sources", help="test that every data source can be downloaded")
    d = sub.add_parser("download", help="download / update all data")
    d.add_argument("--full", action="store_true", help="download everything again from 2015")
    sub.add_parser("check", help="run the data checks")
    sub.add_parser("analyse", help="steps 2-8: charts, tests on normal winters, savings, report")
    r = sub.add_parser("retrain", help="monthly: retrain the daily model (plus refresh analysis)")
    r.add_argument("--force", action="store_true", help="replace the model even if it fails the 4-week check")
    r.add_argument("--skip-analysis", action="store_true")
    dl = sub.add_parser("daily", help="daily: download, forecast tomorrow, score, build page")
    dl.add_argument("--skip-download", action="store_true")
    dl.add_argument("--backfill", type=int, default=0, help="first make forecasts for the last N days")
    sub.add_parser("site", help="rebuild the web page only")
    a = sub.add_parser("all", help="first run: everything in order")
    a.add_argument("--skip-download", action="store_true")
    a.add_argument("--backfill", type=int, default=30)
    dm = sub.add_parser("demo", help="run everything offline on realistic stand-in data")
    dm.add_argument("--dir", default="demo_run")
    dm.add_argument("--full-model", action="store_true", help="use the real model size (slower)")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("matplotlib", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "demo":
        from .demo import run as demo
        os.environ.setdefault("DESAVE_HTTP_PAUSE", "0")
        cfg, _srv = demo.setup(Path(args.dir), quick=not args.full_model)
        (cfg.home / "README.md").write_text("<!-- HEADLINE:START -->\n<!-- HEADLINE:END -->\n")
        cmd_check_sources(cfg)
        cmd_all(cfg)
        print(f"\nDemo finished. Open {cfg.home / 'site' / 'index.html'} and read {cfg.home / 'reports' / 'REPORT.md'}")
        return 0

    cfg = _cfg(args)
    if args.cmd == "check-sources":
        return cmd_check_sources(cfg)
    if args.cmd == "download":
        cmd_download(cfg, args.full)
    elif args.cmd == "check":
        return cmd_check(cfg)
    elif args.cmd == "analyse":
        from .analysis import report
        res = report.full(cfg)
        print(res["verdict"])
    elif args.cmd == "retrain":
        from .pipeline import retrain
        out = retrain.run(cfg, include_analysis=not args.skip_analysis, force=args.force)
        print(json.dumps({k: v.get("gate", v) for k, v in out.items()}, indent=2, default=str))
    elif args.cmd == "daily":
        from .pipeline import daily
        if args.backfill:
            daily.backfill(cfg, args.backfill)
        print(json.dumps(daily.run(cfg, skip_download=args.skip_download), indent=2))
    elif args.cmd == "site":
        from .web import build
        print(build.build(cfg))
    elif args.cmd == "all":
        cmd_all(cfg, args.skip_download, args.backfill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
