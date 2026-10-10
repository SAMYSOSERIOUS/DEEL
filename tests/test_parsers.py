"""Each downloader reads its source's real file format correctly."""
import io
import zipfile

import numpy as np
import pytest
import pandas as pd

from desave import store
from desave.sources import dwd, entsoe, eurostat, opsd, smard, weather_forecast


def test_smard_drops_unpublished_hours():
    payload = {"meta_data": {"version": 1, "created": 1791624970870},
               "series": [[1791151200000, 43871.46], [1791154800000, 42910.0], [1791158400000, None]]}
    s = smard.parse_chunk(payload)
    assert len(s) == 2
    assert s.index[0] == pd.Timestamp("2026-10-04 22:00", tz="UTC")   # Monday 00:00 German summer time
    assert s.iloc[0] == 43871.46


STATIONS = """Stations_id von_datum bis_datum Stationshoehe geoBreite geoLaenge Stationsname Bundesland Abgabe
----------- --------- --------- ------------- --------- --------- ----------------------------------------- ---------- --------
00003 19500401 20110331            202     50.7827    6.0941 Aachen                                   Nordrhein-Westfalen                      Frei
00433 19510101 20261008             48     52.4676   13.4020 Berlin-Tempelhof                         Berlin                                   Frei
00430 19630101 20210504             36     52.5644   13.3088 Berlin-Tegel                             Berlin                                   Frei
01975 19490101 20261008             11     53.6332    9.9881 Hamburg-Fuhlsbüttel                      Hamburg                                  Frei
"""


def test_station_list_and_choice():
    st = dwd.parse_station_list(STATIONS)
    assert len(st) == 4 and st.loc[st.station_id == 433, "name"].iloc[0] == "Berlin-Tempelhof"
    pts = [{"name": "Berlin", "lat": 52.56, "lon": 13.31}]   # right next to Tegel, which closed in 2021
    chosen = dwd.choose_stations(st, pts, "2015-01-01", today=pd.Timestamp("2026-10-10"))
    assert chosen["Berlin"]["station_id"] == 433


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadaten_Geographie_00433.txt", "x")
        z.writestr("produkt_tu_stunde_20250401_20261008_00433.txt", text.encode("latin-1"))
    return buf.getvalue()


def test_dwd_product_zip():
    text = ("STATIONS_ID;MESS_DATUM;QN_9;TT_TU;RF_TU;eor\n"
            "        433;2026100800;    3;  11.2;  88.0;eor\n"
            "        433;2026100801;    3;-999  ;  89.0;eor\n"
            "        433;2026100802;    3;  10.4;  90.0;eor\n")
    s = dwd.parse_product_zip(_zip(text), "TT_TU")
    assert s.index[0] == pd.Timestamp("2026-10-08 00:00", tz="UTC")
    assert np.isnan(s.iloc[1]) and s.iloc[2] == 10.4


def test_entsoe_xml_15min_and_compressed():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<GL_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0">
 <TimeSeries><curveType>A03</curveType>
  <Period><timeInterval><start>2026-10-08T22:00Z</start><end>2026-10-09T00:00Z</end></timeInterval>
   <resolution>PT15M</resolution>
   <Point><position>1</position><quantity>40000</quantity></Point>
   <Point><position>3</position><quantity>44000</quantity></Point>
   <Point><position>5</position><quantity>50000</quantity></Point>
  </Period></TimeSeries></GL_MarketDocument>"""
    s = entsoe.parse_load_xml(xml)
    assert list(s.index) == [pd.Timestamp("2026-10-08 22:00", tz="UTC"), pd.Timestamp("2026-10-08 23:00", tz="UTC")]
    assert s.iloc[0] == (40000 + 40000 + 44000 + 44000) / 4      # missing positions repeat the last value
    assert s.iloc[1] == 50000


def test_entsoe_calibration():
    idx = pd.date_range("2025-01-01", periods=24 * 30, freq="h", tz="UTC")
    e = pd.Series(1000.0, index=idx)
    s = e * 1.07
    assert np.allclose(entsoe.calibrate(s, e).to_numpy(), 1.07)


def test_eurostat_jsonstat():
    payload = {"id": ["freq", "nace_r2", "geo", "time"], "size": [1, 1, 1, 3],
               "dimension": {"time": {"category": {"index": {"2024-01": 0, "2024-02": 1, "2024-03": 2}}}},
               "value": {"0": 78.8, "2": 82.3}}
    s = eurostat.parse_jsonstat(payload)
    assert s[pd.Period("2024-01", "M")] == 78.8 and pd.Period("2024-02", "M") not in s.index


def test_brightsky_sunshine_moved_to_hour_start():
    payload = {"weather": [{"timestamp": "2026-10-10T10:00:00+00:00", "temperature": 12.0, "sunshine": 30},
                           {"timestamp": "2026-10-10T11:00:00+00:00", "temperature": 13.0, "sunshine": 60}]}
    df = weather_forecast.parse_brightsky(payload)
    assert df.loc[pd.Timestamp("2026-10-10 10:00", tz="UTC"), "sun"] == 60   # sun counted 10:00-11:00
    assert df.loc[pd.Timestamp("2026-10-10 10:00", tz="UTC"), "temp"] == 12.0


def test_openmeteo():
    payload = {"hourly": {"time": ["2026-10-10T10:00", "2026-10-10T11:00"], "temperature_2m": [12, 13],
                          "sunshine_duration": [1800, 3600]}}
    df = weather_forecast.parse_openmeteo(payload)
    assert df.loc[pd.Timestamp("2026-10-10 10:00", tz="UTC"), "sun"] == 60


def test_opsd_csv():
    txt = ("utc_timestamp,cet_cest_timestamp,DE_load_actual_entsoe_transparency\n"
           "2015-01-01T00:00:00Z,2015-01-01T01:00:00+0100,41000\n2015-01-01T01:00:00Z,2015-01-01T02:00:00+0100,\n")
    s = opsd.parse_csv(io.StringIO(txt), "DE_load_actual_entsoe_transparency")
    assert len(s) == 1 and s.iloc[0] == 41000


def test_store_monthly_roundtrip(tmp_cfg):
    idx = pd.date_range("2023-01-30", "2023-02-03", freq="h", tz="UTC")
    store.save_monthly(tmp_cfg, "x", pd.DataFrame({"v": 1.0}, index=idx))
    store.save_monthly(tmp_cfg, "x", pd.DataFrame({"v": 2.0}, index=idx[-5:]))   # corrections replace old values
    back = store.load(tmp_cfg, "x")
    assert len(back) == len(idx) and (back["v"].iloc[-5:] == 2.0).all() and (back["v"].iloc[:-5] == 1.0).all()
    assert sorted(p.stem for p in (tmp_cfg.path("data") / "x").glob("*.parquet")) == ["2023-01", "2023-02"]


def test_national_average_handles_missing_station(cfg):
    idx = pd.date_range("2023-01-01", periods=3, freq="h", tz="UTC")
    df = pd.DataFrame({f"temp_{p['name']}": 10.0 for p in cfg["weather_points"]}, index=idx)
    df.iloc[1, 0] = np.nan
    df.iloc[1, 1] = 99.0
    t = dwd.national(df, cfg, "temp")
    assert t.iloc[0] == pytest.approx(10.0) and t.notna().all() and t.iloc[1] > 10.0
