"""The website: everything it needs ships with it, one broken part never blanks
the rest, and the report becomes a real web page."""
import json
import re

import pandas as pd

from desave.web import build
from desave.web.build import GEO, TEMPLATE, VENDOR


def test_only_local_scripts():
    """No chart or 3D library from outside servers (that broke the first version of the page)."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert re.findall(r'<script[^>]*\bsrc="([^"]+)"', html) == ["vendor/three.min.js"]
    assert "Plotly" not in html and "cdn" not in html.lower()
    assert (VENDOR / "three.min.js").stat().st_size > 400_000
    assert (VENDOR / "THREE_LICENSE.txt").exists()


def test_charts_and_3d_are_guarded():
    html = TEMPLATE.read_text(encoding="utf-8")
    for fn in ("function lineChart(", "function barChart(", "function chartFailed(", "const Map2D", "const Grid3D"):
        assert fn in html
    for chart in ("chart-tomorrow", "chart-history", "chart-scores", "chart-savings"):
        assert f'Chart("{chart}"' in html
    for name in ("hero", "kpis", "savings", "regions", "official", "tomorrow", "history", "accuracy", "stage", "areas"):
        assert f'section("{name}"' in html
    assert "catch (e) { console.warn(\"3D map not shown:\"" in html     # falls back to the flat map
    assert "prefers-reduced-motion" in html


def test_test_winters_keep_their_label():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'Object.assign({}, r, {kind: "check"})' in html


def test_both_themes_define_every_token():
    html = TEMPLATE.read_text(encoding="utf-8")
    root = re.search(r":root \{(.*?)\}", html, re.S).group(1)
    tokens = set(re.findall(r"(--[a-z0-9-]+):", root))
    light = re.search(r':root\[data-theme="light"\] \{(.*?)\}', html, re.S).group(1)
    for t in re.findall(r"(--[a-z0-9-]+):", light):
        assert t in tokens, f"{t} only defined for the light theme"


def test_germany_outline():
    geo = json.loads(GEO.read_text())
    main = geo[0]
    lons, lats = [p[0] for p in main], [p[1] for p in main]
    assert len(main) > 150
    assert 5.8 < min(lons) < 6.2 and 14.9 < max(lons) < 15.1      # west and east borders
    assert 47.2 < min(lats) < 47.4 and 54.9 < max(lats) < 55.1    # Alps and Sylt


def test_report_page(tmp_path):
    md = "# Title\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n![Chart](figures/x.png)\n"
    page = build.report_html(md)
    assert "<title>Netzlast Report</title>" in page
    assert '<div class="tablebox"><table>' in page and 'loading="lazy"' in page
    assert 'href="index.html"' in page


def test_build_writes_everything(tmp_cfg):
    pd.DataFrame().to_csv(tmp_cfg.path("results") / "unused.csv")
    out = build.build(tmp_cfg)
    site = out.parent
    for f in ("index.html", "report.html", "data.json", "vendor/three.min.js", ".nojekyll"):
        assert (site / f).exists(), f
    data = json.loads((site / "data.json").read_text())
    assert data["geo"] and len(data["points"]) == 19
    assert len(data["states_geo"]) == 16 and set(data["states_geo"]) == {s["code"] for s in data["hierarchy"]["states"]}
    assert len(data["hierarchy"]["regions"]) == 4 and set(data["areas"]) == {"50Hertz", "TenneT", "Amprion", "TransnetBW"}
    assert abs(sum(p["weight"] for p in data["points"]) - 1) < 0.01
    page = (site / "index.html").read_text()
    assert "/*__DATA__*/null" not in page and '"points"' in page


def test_area_picker_is_honest():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "Measured · SMARD, every hour" in html
    assert "Estimate · from" in html and "Rough estimate" in html
    assert 'opt("", "No public data exists", true)' in html            # villages: shown, but not pretended
    assert "States (estimated)" in html and "Cities (rough estimate)" in html
