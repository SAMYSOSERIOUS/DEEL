"""The monthly job: retrain the daily model on all data, refresh the savings
analysis with the newest months, and rebuild the report and web page.

The "no crisis" model itself does not change (it is trained only on data before
September 2022), but each month adds new real data to compare it with.
"""
from __future__ import annotations

import logging

from .. import dataset
from ..config import Settings
from ..models import operational

log = logging.getLogger(__name__)


def run(cfg: Settings, include_analysis: bool = True, force: bool = False) -> dict:
    load = dataset.load_series(cfg)
    weather = dataset.weather_observed(cfg)
    out = operational.train(cfg, load, weather, force=force)
    if include_analysis:
        from ..analysis import report
        report.full(cfg)
    from ..web import build as site
    site.build(cfg)
    return out
