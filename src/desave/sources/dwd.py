"""DWD (German Weather Service) open data: hourly temperature and sunshine.

Source: Deutscher Wetterdienst, opendata.dwd.de (free, name the source).

Folder layout used (hourly climate observations):
  {base}/air_temperature/historical/stundenwerte_TU_{id}_{from}_{to}_hist.zip
  {base}/air_temperature/recent/stundenwerte_TU_{id}_akt.zip     (last ~500 days, updated daily)
  {base}/air_temperature/recent/TU_Stundenwerte_Beschreibung_Stationen.txt
  same for {base}/sun/ with "SD" instead of "TU".
Inside each zip: ``produkt_*.txt`` with ``;`` separated columns
STATIONS_ID;MESS_DATUM;QN_x;<values>;eor. MESS_DATUM is YYYYMMDDHH in UTC.
Missing values are -999.

Time convention we use: every value is stored at the START of the hour it
describes, like SMARD. Temperature is a reading at that moment. Sunshine is
reported at the END of the hour it was counted over, so we move it back 1 hour.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile

import numpy as np
import pandas as pd

from .. import store
from ..config import Settings
from ..http import get

log = logging.getLogger(__name__)
NAME = "weather_obs"

VARIABLES = {
    # our name: (DWD folder, file code, value column, shift hours back)
    "temp": ("air_temperature", "TU", "TT_TU", 0),
    "sun": ("sun", "SD", "SD_SO", 1),
}


def _base(cfg: Settings) -> str:
    return cfg["sources"]["dwd"]["base_url"].rstrip("/")


# ---------------------------------------------------------------- station list
def parse_station_list(text: str) -> pd.DataFrame:
    """Parse DWD's fixed-width station description file."""
    rows = []
    for line in text.splitlines():
        tok = line.split()
        if len(tok) < 9 or not tok[0].isdigit():
            continue  # header and ruler lines
        rows.append({
            "station_id": int(tok[0]),
            "from": pd.Timestamp(tok[1]),
            "to": pd.Timestamp(tok[2]),
            "height_m": float(tok[3]),
            "lat": float(tok[4]),
            "lon": float(tok[5]),
            "name": " ".join(tok[6:-2]),
            "state": tok[-2],
        })
    return pd.DataFrame(rows)


def station_list(cfg: Settings, var: str) -> pd.DataFrame:
    folder, code, _, _ = VARIABLES[var]
    r = get(f"{_base(cfg)}/{folder}/recent/{code}_Stundenwerte_Beschreibung_Stationen.txt")
    return parse_station_list(r.content.decode("latin-1"))


def _km(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi, dl = p2 - p1, np.radians(np.asarray(lon2) - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def choose_stations(stations: pd.DataFrame, points: list[dict], start: str,
                    recent_days: int = 30, today: pd.Timestamp | None = None) -> dict[str, dict]:
    """For each weather point pick the closest station that has data from ``start`` until now."""
    today = (today or pd.Timestamp.now()).normalize()
    ok = stations[(stations["from"] <= pd.Timestamp(start)) &
                  (stations["to"] >= today - pd.Timedelta(days=recent_days))].copy()
    if ok.empty:
        raise RuntimeError("No DWD station covers the whole period")
    chosen = {}
    for p in points:
        d = _km(p["lat"], p["lon"], ok["lat"].to_numpy(), ok["lon"].to_numpy())
        i = int(np.argmin(d))
        row = ok.iloc[i]
        chosen[p["name"]] = {"station_id": int(row["station_id"]), "station_name": row["name"],
                             "lat": float(row["lat"]), "lon": float(row["lon"]),
                             "distance_km": round(float(d[i]), 1)}
    return chosen


def stations_file(cfg: Settings):
    return cfg.path("data") / "stations.json"


def get_stations(cfg: Settings, refresh: bool = False) -> dict:
    """Station choice is saved once so it stays the same every day (a changing station = a changing input)."""
    f = stations_file(cfg)
    if f.exists() and not refresh:
        return json.loads(f.read_text())
    out = {}
    for var in VARIABLES:
        out[var] = choose_stations(station_list(cfg, var), cfg["weather_points"], cfg["project"]["start"])
    f.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    return out


# ------------------------------------------------------------------ zip parsing
def parse_product_zip(content: bytes, value_col: str) -> pd.Series:
    """Read the ``produkt_*.txt`` file from a DWD zip into a UTC-indexed series."""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        name = next(n for n in z.namelist() if n.lower().startswith("produkt"))
        raw = z.read(name).decode("latin-1")
    df = pd.read_csv(io.StringIO(raw), sep=";", dtype=str, skipinitialspace=True)
    df.columns = [c.strip() for c in df.columns]
    t = pd.to_datetime(df["MESS_DATUM"].str.strip().str[:10], format="%Y%m%d%H", utc=True)
    v = pd.to_numeric(df[value_col].str.strip(), errors="coerce")
    v[v <= -999] = np.nan
    s = pd.Series(v.to_numpy(dtype="float64"), index=pd.DatetimeIndex(t)).sort_index()
    return s[~s.index.duplicated(keep="last")]


def _historical_name(cfg: Settings, folder: str, code: str, station_id: int) -> str | None:
    r = get(f"{_base(cfg)}/{folder}/historical/")
    pat = re.compile(rf'(stundenwerte_{code}_{station_id:05d}_\d{{8}}_\d{{8}}_hist\.zip)')
    names = sorted(set(pat.findall(r.text)))
    return names[-1] if names else None


def station_series(cfg: Settings, var: str, station_id: int, include_historical: bool = True) -> pd.Series:
    folder, code, col, shift = VARIABLES[var]
    parts = []
    if include_historical:
        name = _historical_name(cfg, folder, code, station_id)
        if name:
            r = get(f"{_base(cfg)}/{folder}/historical/{name}", timeout=180)
            parts.append(parse_product_zip(r.content, col))
    r = get(f"{_base(cfg)}/{folder}/recent/stundenwerte_{code}_{station_id:05d}_akt.zip", ok_404=True)
    if r is not None:
        parts.append(parse_product_zip(r.content, col))
    if not parts:
        return pd.Series(dtype="float64", index=pd.DatetimeIndex([], tz="UTC"))
    s = pd.concat(parts).sort_index()
    s = s[~s.index.duplicated(keep="last")]  # "recent" wins over "historical"
    if shift:
        s.index = s.index - pd.Timedelta(hours=shift)
    return s


def update(cfg: Settings, full: bool = False) -> pd.DataFrame:
    """Download temperature and sunshine for every weather point and store them (wide table)."""
    stations = get_stations(cfg)
    has_data = store.last_time(cfg, NAME) is not None
    include_hist = full or not has_data
    cols = {}
    for var, mapping in stations.items():
        for point, info in mapping.items():
            log.info("DWD %s: %s (station %s)", var, point, info["station_id"])
            cols[f"{var}_{point}"] = station_series(cfg, var, info["station_id"], include_hist)
    df = pd.DataFrame(cols).sort_index()
    start = pd.Timestamp(cfg["project"]["start"]).tz_localize(cfg.tz).tz_convert("UTC") - pd.Timedelta(days=3)
    df = df[df.index >= start]
    df.index.name = "time_utc"
    store.save_monthly(cfg, NAME, df)
    return df


# ------------------------------------------------------------- national values
def point_weights(cfg: Settings) -> pd.Series:
    w = pd.Series({p["name"]: float(p["pop_k"]) for p in cfg["weather_points"]})
    return w / w.sum()


def national(df: pd.DataFrame, cfg: Settings, var: str) -> pd.Series:
    """Population-weighted average over the weather points.

    If some stations are missing for an hour, the weights of the ones that
    are there are rescaled, so one broken station doesn't create a gap.
    """
    w = point_weights(cfg)
    cols = [f"{var}_{p}" for p in w.index if f"{var}_{p}" in df.columns]
    if not cols:
        return pd.Series(np.nan, index=df.index)
    vals = df[cols].to_numpy(dtype="float64")
    ww = w.reindex([c.split("_", 1)[1] for c in cols]).to_numpy()
    mask = ~np.isnan(vals)
    num = np.nansum(vals * ww, axis=1)
    den = (mask * ww).sum(axis=1)
    out = np.where(den > 0.5, num / np.where(den == 0, 1, den), np.nan)  # need >50% of people covered
    return pd.Series(out, index=df.index, name=var)


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    return float(_km(lat1, lon1, np.array([lat2]), np.array([lon2]))[0])

