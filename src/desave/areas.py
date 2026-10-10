"""The area hierarchy: Germany -> 4 grid regions -> 16 states -> cities.

Measured (hourly, SMARD):   Germany and the 4 grid regions (control areas of the
                            transmission grid companies 50Hertz, TenneT, Amprion, TransnetBW).
Estimated:                  states = their grid region × the state's share of the region's people;
                            cities = their state × the city's share of the state's people.
No public data:             towns and villages (smart-meter data is private).

For the measured areas everything runs separately: download, data checks,
weather (only the stations in that area), holidays (only that area's states),
the savings analysis with ranges, the daily forecast, its scores and range.
"""
from __future__ import annotations

import copy

from .config import Settings

COUNTRY = "DE"


def regions(cfg: Settings) -> list[dict]:
    return list(cfg.get("regions") or [])


def region_codes(cfg: Settings) -> list[str]:
    return [r["code"] for r in regions(cfg)]


def measured(cfg: Settings) -> list[str]:
    """Germany first, then the grid regions."""
    return [COUNTRY] + region_codes(cfg)


def region_of_state(cfg: Settings, state: str) -> str | None:
    return next((r["code"] for r in regions(cfg) if state in r["states"]), None)


def scoped(cfg: Settings, code: str) -> Settings:
    """Settings for one measured area: its own stations, holidays, SMARD region and folders."""
    if code == COUNTRY:
        return cfg
    reg = next((r for r in regions(cfg) if r["code"] == code), None)
    if reg is None:
        raise KeyError(f"Unknown area {code!r}")
    raw = copy.deepcopy(cfg.raw)
    raw["area"] = code
    raw["weather_points"] = [p for p in cfg["weather_points"] if p["state"] in reg["states"]]
    if not raw["weather_points"]:
        raise ValueError(f"No weather stations inside {code}")
    raw["sources"]["smard"]["region"] = code
    # smaller models for the regions, but never bigger than the main one (keeps quick demos quick)
    over = cfg.get("regional_model") or {}
    lgb = raw["model"]["lightgbm"]
    if over.get("lightgbm", {}).get("n_estimators", 10**9) < lgb.get("n_estimators", 10**9):
        lgb.update(over["lightgbm"])
    if over.get("random_seeds") and len(over["random_seeds"]) < len(raw["model"]["random_seeds"]):
        raw["model"]["random_seeds"] = list(over["random_seeds"])
    return Settings(raw, cfg.home)


def population(cfg: Settings) -> dict:
    """People (thousands) per state, per region and in Germany, from the weather points' weights."""
    states: dict[str, float] = {}
    for p in cfg["weather_points"]:
        states[p["state"]] = states.get(p["state"], 0.0) + float(p["pop_k"])
    regs = {r["code"]: sum(states.get(s, 0.0) for s in r["states"]) for r in regions(cfg)}
    return {"states": states, "regions": regs, "country": sum(states.values())}


def tree(cfg: Settings) -> dict:
    """Everything the website needs to draw the hierarchy, with each estimate's share spelled out."""
    pop = population(cfg)
    names = cfg.get("state_names") or {}
    cities = cfg.get("cities") or {}
    out = {"regions": [], "states": [], "cities": []}
    for r in regions(cfg):
        out["regions"].append({"code": r["code"], "name": r["name"], "area_name": r.get("area_name", ""),
                               "states": r["states"], "pop_k": round(pop["regions"][r["code"]])})
    for st, pk in sorted(pop["states"].items()):
        reg = region_of_state(cfg, st)
        out["states"].append({"code": st, "name": names.get(st, st), "region": reg, "pop_k": round(pk),
                              "share_of_region": round(pk / pop["regions"][reg], 4) if reg else None})
    state_pop = pop["states"]
    for p in cfg["weather_points"]:
        c = cities.get(p["name"])
        if not c:
            continue
        share = min(1.0, c["pop_k"] / state_pop[p["state"]])
        out["cities"].append({"code": p["name"], "name": c["name"], "state": p["state"],
                              "region": region_of_state(cfg, p["state"]), "pop_k": c["pop_k"],
                              "share_of_state": round(share, 4), "lat": p["lat"], "lon": p["lon"]})
    return out
