"""The web page must load its chart library from a working address, and one
broken chart must never blank the rest of the page."""
import re

from desave.web.build import TEMPLATE


def test_chart_library_address():
    html = TEMPLATE.read_text(encoding="utf-8")
    srcs = re.findall(r'<script src="([^"]+)"', html)
    # the cdnjs address used at first does not exist; the npm package is published here
    assert srcs[0] == "https://cdn.jsdelivr.net/npm/plotly.js-dist-min@2.35.2/plotly.min.js"
    assert not any("cdnjs" in s for s in srcs)
    assert "unpkg.com/plotly.js-dist-min@2.35.2" in html          # second source if the first fails


def test_sections_are_independent():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert "Plotly.newPlot(id" in html and html.count("Plotly.newPlot") == 1   # only inside the safe helper
    for name in ("tomorrow", "history", "scores", "savings"):
        assert f'section("{name}"' in html
    assert "/*__DATA__*/null" in html
