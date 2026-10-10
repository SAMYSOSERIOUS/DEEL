"""Set up a self-contained demo folder: fake internet + its own settings + data."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml

from ..config import Settings, load_settings
from . import mock_server, synthetic

log = logging.getLogger(__name__)

QUICK_MODEL = {"model": {"lightgbm": {"n_estimators": 400, "learning_rate": 0.06}, "random_seeds": [1]}}


def setup(folder: Path, quick: bool = True, regenerate: bool = False) -> tuple[Settings, object]:
    """Create ``folder`` with a settings file pointing at a local fake server, and start that server."""
    folder = Path(folder).resolve()
    (folder / "config").mkdir(parents=True, exist_ok=True)
    base_cfg = load_settings(Path(__file__).resolve().parents[3])
    mock = folder / "mock_internet"
    if regenerate and mock.exists():
        shutil.rmtree(mock)
    if not (mock / "smard").exists():
        log.info("Writing stand-in data (about 30 seconds)...")
        synthetic.generate(base_cfg, mock)
    srv, url = mock_server.start(mock, base_cfg["weather_points"])
    over = mock_server.demo_overrides(url)
    if quick:
        over = _merge(over, QUICK_MODEL)
    cfg = base_cfg.with_overrides(over)
    raw = dict(cfg.raw)
    raw["demo"] = {"mock_folder": str(mock), "server": url}
    (folder / "config" / "settings.yaml").write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    return Settings(raw, folder), srv


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out
