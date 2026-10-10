"""Realistic stand-in data, written in the EXACT file formats of SMARD and DWD.

Purpose:
  1. Try the whole project without internet.
  2. Prove the method works: we plant a known saving into the fake data and
     check that the analysis finds it (see tests/test_planted_saving.py).

The fake German demand includes everything the real one has: daily and weekly
rhythm, temperature (heating), holidays per state, Christmas, the 2020
lockdowns, a slow downward trend, rooftop solar that hides daytime demand and
grows every year, clock changes, and a planted crisis saving from September 2022.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .. import features as F
from ..config import Settings
from ..timeutils import local_index, sun_elevation_deg


@dataclass
class Truth:
    """The planted crisis effect, so tests can compare the estimate with the truth."""
    start: str = "2022-09-01"
    peak: float = 0.06          # 6% less demand at the peak (winter 2022/23)
    lasting: float = 0.035      # settles at 3.5% less from 2024 on
    trend_per_year: float = -0.003
    # each grid region saves a different amount, so tests can check every region is found correctly
    regions: dict = field(default_factory=lambda: {
        "50Hertz": (0.045, 0.025), "TenneT": (0.06, 0.035), "Amprion": (0.08, 0.05), "TransnetBW": (0.05, 0.03)})

    def for_region(self, code: str) -> "Truth":
        peak, lasting = self.regions.get(code, (self.peak, self.lasting))
        return Truth(self.start, peak, lasting, self.trend_per_year, dict(self.regions))


def crisis_factor(idx: pd.DatetimeIndex, truth: Truth) -> np.ndarray:
    t = (idx - pd.Timestamp(truth.start, tz="UTC")) / pd.Timedelta(days=365.25)
    t = np.asarray(t, dtype="float64")
    ramp = np.clip(t / (30 / 365.25), 0, 1)                     # builds up over September 2022
    decay = truth.lasting + (truth.peak - truth.lasting) * np.exp(-np.clip(t - 0.6, 0, None) / 0.7)
    return np.where(t < 0, 1.0, 1.0 - ramp * decay)


def make_weather(cfg: Settings, idx: pd.DatetimeIndex, seed: int = 7) -> dict[str, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    n = len(idx)
    loc = local_index(idx, cfg.tz)
    doy = loc.dayofyear.to_numpy()
    hour = loc.hour.to_numpy()
    # shared weather swings (cold spells, warm spells) lasting days
    common = np.zeros(n)
    eps = rng.normal(0, 0.55, n)
    for i in range(1, n):
        common[i] = 0.9965 * common[i - 1] + eps[i]
    cloud_common = np.zeros(n)
    eps = rng.normal(0, 0.08, n)
    for i in range(1, n):
        cloud_common[i] = 0.98 * cloud_common[i - 1] + eps[i]
    out = {}
    for p in cfg["weather_points"]:
        south = (52.5 - p["lat"]) * 0.4
        seasonal = 9.6 + 9.8 * np.sin(2 * np.pi * (doy - 110) / 365.25) + south * np.sin(2 * np.pi * (doy - 20) / 365.25)
        diurnal = 3.2 * np.sin(2 * np.pi * (hour - 9) / 24)
        local = rng.normal(0, 0.6, n)
        temp = seasonal + diurnal + common * (1 + 0.1 * rng.normal()) + local
        elev = sun_elevation_deg(idx, p["lat"], p["lon"])
        clear = np.clip(1.0 - (0.45 + cloud_common + rng.normal(0, 0.15, n)), 0, 1)
        sun = np.where(elev > 3, 60 * clear, 0.0).round(0)
        out[p["name"]] = pd.DataFrame({"temp": temp.round(1), "sun": sun}, index=idx)
    return out


def make_load(cfg: Settings, idx: pd.DatetimeIndex, weather: dict[str, pd.DataFrame], truth: Truth,
              seed: int = 11, scale: float = 1.0) -> pd.DataFrame:
    """Returns the fake demand AND the same demand without the crisis (the truth to compare with)."""
    rng = np.random.default_rng(seed)
    w = pd.Series({p["name"]: p["pop_k"] for p in cfg["weather_points"]}, dtype="float64")
    w /= w.sum()
    temp = sum(weather[k]["temp"] * w[k] for k in w.index)
    sun = sum(weather[k]["sun"] * w[k] for k in w.index)
    X = F.build(cfg, idx, pd.DataFrame({"temp": temp, "sun": sun}))
    hour, dow = X["hour"].to_numpy(), X["dow"].to_numpy()
    # daily rhythm: night low, morning rise, midday, evening peak
    prof_wd = np.array([.78, .75, .74, .74, .76, .82, .92, 1.02, 1.07, 1.09, 1.10, 1.11,
                        1.10, 1.09, 1.07, 1.05, 1.05, 1.08, 1.12, 1.11, 1.05, .98, .90, .83])
    prof_we = prof_wd * np.array([1.0] * 6 + [0.88] * 12 + [0.97] * 6)
    weekend_level = np.where(dow == 5, 0.88, np.where(dow == 6, 0.80, 1.0))
    prof = np.where(dow >= 5, prof_we[hour], prof_wd[hour]) * weekend_level
    base = 57_500 * scale * prof
    hdd = np.clip(15 - X["temp_daily"].to_numpy(), 0, None)
    cdd = np.clip(X["temp_24h"].to_numpy() - 21, 0, None)
    weather_mult = 1 + 0.0085 * hdd + 0.006 * cdd
    lighting = 1 + 0.04 * (X["sun_elev"].to_numpy() < 0) * (X["hour"].between(16, 22)).to_numpy()
    cal = (1 - 0.20 * X["holiday_share"].to_numpy() * (dow < 5)
           - 0.06 * X["bridge_share"].to_numpy() - 0.07 * X["between_years"].to_numpy()
           - 0.05 * X["is_dec24"].to_numpy() - 0.04 * X["is_dec31"].to_numpy()
           - 0.01 * X["school_share"].to_numpy())
    covid = 1 - 0.08 * X["covid"].to_numpy()
    years = np.asarray((idx - pd.Timestamp("2015-01-01", tz="UTC")) / pd.Timedelta(days=365.25), dtype="float64")
    trend = 1 + truth.trend_per_year * years
    core = base * weather_mult * lighting * cal * covid * trend
    # rooftop solar used at home never reaches the grid: grows from ~1 GW to ~9 GW peak
    pv_gw = 1.0 + 0.7 * years
    pv = 1000 * scale * pv_gw * (sun.to_numpy() / 60.0) * 0.6
    noise = np.zeros(len(idx))
    eps = rng.normal(0, 0.006, len(idx))
    for i in range(1, len(idx)):
        noise[i] = 0.95 * noise[i - 1] + eps[i]
    load = (core * crisis_factor(idx, truth) - pv) * (1 + noise)
    no_crisis = (core - pv) * (1 + noise)
    return pd.DataFrame({"load_mwh": load.round(2), "no_crisis_mwh": no_crisis.round(2)}, index=idx)


# ------------------------------------------------------------------ writers
def write_smard(folder: Path, load: pd.Series, tz: str, filt: int = 410, region: str = "DE",
                publish_until: pd.Timestamp | None = None) -> None:
    d = folder / "smard" / str(filt) / region
    d.mkdir(parents=True, exist_ok=True)
    loc = load.index.tz_convert(tz)
    first_monday = (loc[0].normalize() - pd.Timedelta(days=loc[0].dayofweek)).tz_localize(None)
    last = loc[-1].tz_localize(None)
    starts = []
    m = first_monday
    while m <= last:
        s_utc = pd.Timestamp(m).tz_localize(tz).tz_convert("UTC")
        e_utc = pd.Timestamp(m + pd.Timedelta(days=7)).tz_localize(tz).tz_convert("UTC")
        hours = pd.date_range(s_utc, e_utc, freq="h", inclusive="left")
        vals = load.reindex(hours)
        if publish_until is not None:
            vals[hours >= publish_until] = np.nan
        series = [[int(t.value // 10**6), (None if pd.isna(v) else float(v))] for t, v in zip(hours, vals)]
        ms = int(s_utc.value // 10**6)
        (d / f"{filt}_{region}_hour_{ms}.json").write_text(
            json.dumps({"meta_data": {"version": 1, "created": ms}, "series": series}))
        starts.append(ms)
        m += pd.Timedelta(days=7)
    (d / "index_hour.json").write_text(json.dumps({"timestamps": starts}))


def _station_line(sid: int, frm: str, to: str, lat: float, lon: float, name: str, state: str) -> str:
    return f"{sid:05d} {frm} {to} {50:>14} {lat:>11.4f} {lon:>9.4f} {name:<40} {state:<40} Frei"


STATE_NAMES = {"BE": "Berlin", "HH": "Hamburg", "BY": "Bayern", "NW": "Nordrhein-Westfalen",
               "BW": "Baden-Württemberg", "NI": "Niedersachsen", "HE": "Hessen", "SN": "Sachsen",
               "RP": "Rheinland-Pfalz", "SH": "Schleswig-Holstein", "BB": "Brandenburg",
               "ST": "Sachsen-Anhalt", "TH": "Thüringen", "MV": "Mecklenburg-Vorpommern",
               "SL": "Saarland", "HB": "Bremen"}


def write_dwd(folder: Path, cfg: Settings, weather: dict[str, pd.DataFrame], hist_end: str,
              recent_start: str, obs_until: pd.Timestamp) -> dict[str, int]:
    """Write DWD-style station lists and zips for temperature (TU) and sunshine (SD)."""
    ids = {}
    for var, code, folder_name, qn, col in (("temp", "TU", "air_temperature", "QN_9", "TT_TU"),
                                            ("sun", "SD", "sun", "QN_7", "SD_SO")):
        base = folder / "dwd" / folder_name
        (base / "historical").mkdir(parents=True, exist_ok=True)
        (base / "recent").mkdir(parents=True, exist_ok=True)
        lines = ["Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe",
                 "----------- --------- --------- ------------- --------- --------- ----------------------------------------- ---------- --------"]
        links = []
        today = pd.Timestamp.now().strftime("%Y%m%d")
        for i, p in enumerate(cfg["weather_points"]):
            sid = 400 + i * 7 + (0 if var == "temp" else 1)
            ids[f"{var}_{p['name']}"] = sid
            st = STATE_NAMES[p["state"]]
            lines.append(_station_line(sid, "19900101", today, p["lat"] + 0.03, p["lon"] - 0.02,
                                       f"{p['name']}-Mitte", st))
            # a closer station that closed years ago: must NOT be chosen
            lines.append(_station_line(sid + 3, "19800101", "20120630", p["lat"], p["lon"], f"{p['name']}-Alt", st))
            df = weather[p["name"]]
            vals = df[var]
            if var == "sun":  # DWD stamps sunshine at the END of the hour
                vals = vals.copy()
                vals.index = vals.index + pd.Timedelta(hours=1)
            vals = vals[vals.index < obs_until]
            for kind, part in (("hist", vals[vals.index < pd.Timestamp(hist_end, tz="UTC")]),
                               ("akt", vals[vals.index >= pd.Timestamp(recent_start, tz="UTC")])):
                if part.empty:
                    continue
                part = part.copy()
                if kind == "hist":  # a few missing readings, as in real data
                    part.iloc[1000:1003] = np.nan
                buf = io.StringIO()
                buf.write(f"STATIONS_ID;MESS_DATUM;{qn};{col};eor\n")
                for t, v in part.items():
                    vv = -999 if pd.isna(v) else v
                    buf.write(f"{sid:>11};{t.strftime('%Y%m%d%H')};    3;{vv:>6};eor\n")
                f0, f1 = part.index.min().strftime("%Y%m%d"), part.index.max().strftime("%Y%m%d")
                inner = f"produkt_{code.lower()}_stunde_{f0}_{f1}_{sid:05d}.txt"
                if kind == "hist":
                    zname = f"stundenwerte_{code}_{sid:05d}_{f0}_{f1}_hist.zip"
                    target = base / "historical" / zname
                    links.append(zname)
                else:
                    target = base / "recent" / f"stundenwerte_{code}_{sid:05d}_akt.zip"
                with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
                    z.writestr(inner, buf.getvalue().encode("latin-1"))
                    z.writestr(f"Metadaten_Geographie_{sid:05d}.txt", "dummy")
        (base / "recent" / f"{code}_Stundenwerte_Beschreibung_Stationen.txt").write_bytes(
            "\n".join(lines).encode("latin-1"))
        (base / "historical" / "index.html").write_text(
            "<html><body>" + "".join(f'<a href="{n}">{n}</a>\n' for n in links) + "</body></html>")
    return ids


def write_opsd(folder: Path, load: pd.Series) -> None:
    s = load[(load.index >= "2015-01-01") & (load.index < "2020-10-01")]
    rng = np.random.default_rng(3)
    ent = s * 0.93 * (1 + rng.normal(0, 0.01, len(s)))  # different definition + small differences
    df = pd.DataFrame({"utc_timestamp": s.index.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "cet_cest_timestamp": s.index.tz_convert("Europe/Berlin").strftime("%Y-%m-%dT%H:%M:%S%z"),
                       "DE_load_actual_entsoe_transparency": ent.round(1).to_numpy()})
    (folder / "opsd").mkdir(parents=True, exist_ok=True)
    df.to_csv(folder / "opsd" / "time_series_60min_singleindex.csv", index=False)


def write_eurostat(folder: Path, truth: Truth, until: pd.Timestamp) -> None:
    months = pd.period_range("2015-01", (until - pd.Timedelta(days=45)).strftime("%Y-%m"), freq="M")
    d = folder / "eurostat"
    d.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(5)
    for code, depth in (("C17", 0.10), ("C20", 0.18), ("C23", 0.12), ("C24", 0.10), ("B-D", 0.05)):
        t = np.array([(p.to_timestamp() - pd.Timestamp(truth.start)).days / 365.25 for p in months])
        drop = np.where(t < -0.3, 0, depth * np.clip((t + 0.3) / 0.4, 0, 1) * (0.7 + 0.3 * np.exp(-np.clip(t, 0, None))))
        idx = 100 * (1 - drop) * (1 + rng.normal(0, 0.015, len(months)))
        payload = {"version": "2.0", "class": "dataset", "id": ["freq", "indic_bt", "nace_r2", "s_adj", "unit", "geo", "time"],
                   "size": [1, 1, 1, 1, 1, 1, len(months)],
                   "dimension": {"time": {"category": {"index": {str(p): i for i, p in enumerate(months)}}}},
                   "value": {str(i): round(float(v), 1) for i, v in enumerate(idx)}}
        (d / f"{code}.json").write_text(json.dumps(payload))


def generate(cfg: Settings, folder: Path, truth: Truth | None = None, now: pd.Timestamp | None = None) -> dict:
    """Write a complete fake internet into ``folder``. Returns paths and truth.

    Each grid region is made on its own (its weather stations, its states' holidays, its own
    planted saving); Germany is the sum of the four, as in reality.
    """
    from .. import areas
    truth = truth or Truth()
    now = (now or pd.Timestamp.now(tz="UTC")).floor("h")
    start = pd.Timestamp(cfg["project"]["start"]).tz_localize(cfg.tz).tz_convert("UTC") - pd.Timedelta(days=7)
    idx = pd.date_range(start, now + pd.Timedelta(days=4), freq="h")
    weather = make_weather(cfg, idx)
    pop = areas.population(cfg)
    parts = {}
    for i, code in enumerate(areas.region_codes(cfg)):
        rc = areas.scoped(cfg, code)
        parts[code] = make_load(rc, idx, {p["name"]: weather[p["name"]] for p in rc["weather_points"]},
                                truth.for_region(code), seed=20 + i, scale=pop["regions"][code] / pop["country"])
    both = sum(parts.values()) if parts else make_load(cfg, idx, weather, truth)
    load = both["load_mwh"]
    obs_until = now.normalize() - pd.Timedelta(hours=1)          # weather measured until last night
    publish_until = now.normalize() - pd.Timedelta(hours=2)       # demand published until late yesterday
    first = pd.Timestamp(cfg["project"]["start"]).tz_localize(cfg.tz).tz_convert("UTC")
    real_load = load[load.index >= first]
    real_load = real_load.drop(real_load.index[50_000])          # one missing hour, as can happen
    write_smard(folder, real_load, cfg.tz, publish_until=publish_until)
    for code, part in parts.items():
        write_smard(folder, part["load_mwh"][part.index >= first], cfg.tz, region=code, publish_until=publish_until)
    hist_end = f"{now.year}-01-01"
    recent_start = (now - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
    write_dwd(folder, cfg, weather, hist_end, recent_start, obs_until)
    write_opsd(folder, load)
    write_eurostat(folder, truth, now)
    truth_dir = folder / "_truth"
    truth_dir.mkdir(exist_ok=True)
    pd.DataFrame({p: weather[p]["temp"] for p in weather}).to_parquet(truth_dir / "temp.parquet")
    pd.DataFrame({p: weather[p]["sun"] for p in weather}).to_parquet(truth_dir / "sun.parquet")
    both.to_parquet(truth_dir / "load.parquet")
    for code, part in parts.items():
        part.to_parquet(truth_dir / f"load_{code}.parquet")
    pd.Series(crisis_factor(idx, truth), index=idx).to_frame("factor").to_parquet(truth_dir / "crisis_factor.parquet")
    (truth_dir / "truth.json").write_text(json.dumps(truth.__dict__))
    return {"folder": str(folder), "now": str(now)}


def true_winter_saving(folder: Path, cfg: Settings, winter: str, area: str = "DE") -> float:
    """The planted saving for a winter, as the analysis would measure it (% of 'no crisis' demand)."""
    from ..timeutils import winter_bounds
    both = pd.read_parquet(folder / "_truth" / ("load.parquet" if area == "DE" else f"load_{area}.parquet"))
    ws, we = winter_bounds(winter, cfg["analysis"]["winter_months"], cfg.tz)
    m = (both.index >= ws) & (both.index < we)
    return float(100 * (1 - both.loc[m, "load_mwh"].sum() / both.loc[m, "no_crisis_mwh"].sum()))
