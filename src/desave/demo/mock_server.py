"""A tiny local web server that pretends to be SMARD, DWD, Bright Sky,
Open-Meteo, Eurostat and OPSD, serving the stand-in data from a folder.

The real download code talks to it over HTTP exactly as it would talk to the
real websites, so the download and parsing code gets tested too.
"""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd


class Handler(SimpleHTTPRequestHandler):
    root: Path

    def log_message(self, *args):  # keep test output quiet
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _nearest_point(self, lat: float, lon: float) -> str:
        coords = json.loads((self.root / "_truth" / "points.json").read_text())
        return min(coords, key=lambda p: (coords[p][0] - lat) ** 2 + (coords[p][1] - lon) ** 2)

    def _forecast(self, lat, lon, start, end):
        point = self._nearest_point(lat, lon)
        t = pd.read_parquet(self.root / "_truth" / "temp.parquet")[point]
        s = pd.read_parquet(self.root / "_truth" / "sun.parquet")[point]
        sel = (t.index >= start) & (t.index < end)
        rng = np.random.default_rng(int(abs(lat * 1000 + lon)) % 10_000)
        noise = rng.normal(0, 1.0, sel.sum())  # forecasts are never perfect
        return t[sel] + noise, s[sel]

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path.startswith("/brightsky/weather"):
            start = pd.Timestamp(q["date"])
            start = start.tz_localize("UTC") if start.tz is None else start.tz_convert("UTC")
            end = pd.Timestamp(q.get("last_date", start + pd.Timedelta(days=1)))
            end = end.tz_localize("UTC") if end.tz is None else end.tz_convert("UTC")
            temp, sun = self._forecast(float(q["lat"]), float(q["lon"]), start, end + pd.Timedelta(hours=1))
            sun_end = sun.copy()
            sun_end.index = sun_end.index + pd.Timedelta(hours=1)  # Bright Sky: sunshine in the PREVIOUS hour
            rows = [{"timestamp": t.isoformat(), "source_id": 1, "temperature": round(float(v), 1),
                     "sunshine": (None if pd.isna(sun_end.get(t)) else float(sun_end.get(t)))}
                    for t, v in temp.items()]
            return self._json({"weather": rows, "sources": [{"id": 1, "observation_type": "forecast"}]})
        if u.path.startswith("/openmeteo"):
            now = pd.Timestamp.now(tz="UTC").normalize()
            start = now - pd.Timedelta(days=int(q.get("past_days", 0)))
            end = now + pd.Timedelta(days=int(q.get("forecast_days", 7)))
            temp, sun = self._forecast(float(q["latitude"]), float(q["longitude"]), start, end)
            sun_end = sun.reindex(temp.index - pd.Timedelta(hours=1)).to_numpy()
            return self._json({"hourly": {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in temp.index],
                                          "temperature_2m": [round(float(v), 1) for v in temp],
                                          "sunshine_duration": [None if pd.isna(v) else float(v) * 60 for v in sun_end]}})
        if u.path.startswith("/eurostat/sts_inpr_m"):
            f = self.root / "eurostat" / f"{q.get('nace_r2')}.json"
            if not f.exists():
                return self._json({"error": "not found"}, 404)
            return self._json(json.loads(f.read_text()))
        if u.path.rstrip("/").endswith("/historical"):
            self.path = u.path.rstrip("/") + "/index.html"
        return super().do_GET()


def start(root: Path, cfg_points: list[dict], port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    root = Path(root).resolve()
    (root / "_truth").mkdir(parents=True, exist_ok=True)
    (root / "_truth" / "points.json").write_text(json.dumps({p["name"]: [p["lat"], p["lon"]] for p in cfg_points}))
    handler = partial(Handler, directory=str(root))
    Handler.root = root
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def demo_overrides(base: str) -> dict:
    return {"sources": {
        "smard": {"base_url": f"{base}/smard"},
        "dwd": {"base_url": f"{base}/dwd"},
        "brightsky": {"base_url": f"{base}/brightsky"},
        "openmeteo": {"base_url": f"{base}/openmeteo"},
        "eurostat": {"base_url": f"{base}/eurostat"},
        "opsd": {"url": f"{base}/opsd/time_series_60min_singleindex.csv"},
    }}
