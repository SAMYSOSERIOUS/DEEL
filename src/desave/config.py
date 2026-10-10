"""Load settings and work out where files live.

The project folder is found from the DESAVE_HOME environment variable, or by
walking up from the current folder until a ``config/settings.yaml`` is found.
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SETTINGS = PACKAGE_DIR.parents[1] / "config" / "settings.yaml"


def find_home(start: Path | None = None) -> Path:
    env = os.environ.get("DESAVE_HOME")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / "config" / "settings.yaml").exists():
            return p
    return DEFAULT_SETTINGS.parents[1]


@dataclass
class Settings:
    raw: dict[str, Any]
    home: Path

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def area(self) -> str:
        """Which area these settings describe: "DE" (Germany) or a grid region code like "TenneT"."""
        return self.raw.get("area", "DE")

    @property
    def load_store(self) -> str:
        """Where this area's demand is stored. Germany keeps the original name, so old data stays valid."""
        return "load" if self.area == "DE" else f"load_{self.area}"

    def path(self, name: str) -> Path:
        p = self.home / self.raw["paths"][name]
        if self.area != "DE" and name in ("results", "models"):
            p = p / "areas" / self.area
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def tz(self) -> str:
        return self.raw["project"]["timezone"]

    def with_overrides(self, overrides: dict[str, Any]) -> "Settings":
        """Return a copy with some values replaced (used by tests and the demo)."""
        new = copy.deepcopy(self.raw)
        _deep_update(new, overrides)
        return Settings(new, self.home)


def _deep_update(base: dict, upd: dict) -> None:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v


def load_settings(home: Path | str | None = None) -> Settings:
    home = Path(home).resolve() if home else find_home()
    f = home / "config" / "settings.yaml"
    if not f.exists():
        f = DEFAULT_SETTINGS
    with open(f, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Settings(raw, home)


@lru_cache(maxsize=1)
def settings() -> Settings:
    return load_settings()
