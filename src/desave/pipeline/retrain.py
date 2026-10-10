"""The monthly job: retrain the daily model on all data, for Germany and each grid
region, refresh the savings analysis with the newest months, and rebuild the
report and web page.

The "no crisis" models themselves do not change (they are trained only on data
before September 2022), but each month adds new real data to compare them with.
"""
from __future__ import annotations

import logging

from .. import areas, dataset
from ..config import Settings
from ..models import operational

log = logging.getLogger(__name__)


def run(cfg: Settings, include_analysis: bool = True, force: bool = False) -> dict:
    out = {}
    for code in areas.measured(cfg):
        c = areas.scoped(cfg, code)
        try:
            out[code] = operational.train(c, dataset.load_series(c), dataset.weather_observed(c), force=force)
        except Exception as e:  # noqa: BLE001 - Germany must work; a region problem is reported
            if code == areas.COUNTRY:
                raise
            out[code] = {"error": f"{type(e).__name__}: {e}"}
            log.warning("Retrain for %s failed: %s", code, e)
    if include_analysis:
        from ..analysis import report
        report.full(cfg)
    from ..web import build as site
    site.build(cfg)
    return out
