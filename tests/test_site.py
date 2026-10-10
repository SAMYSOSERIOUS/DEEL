"""The web page draws its charts itself (no outside chart library that could fail
to load), and one broken chart must never blank the rest of the page."""
import re

from desave.web.build import TEMPLATE


def test_no_outside_scripts():
    html = TEMPLATE.read_text(encoding="utf-8")
    assert re.findall(r"<script[^>]*\bsrc=", html) == []
    assert "Plotly" not in html


def test_charts_are_drawn_by_the_page():
    html = TEMPLATE.read_text(encoding="utf-8")
    for fn in ("function lineChart(", "function barChart(", "function chartFailed("):
        assert fn in html
    for chart in ("chart-tomorrow", "chart-history", "chart-scores", "chart-savings"):
        assert f'Chart("{chart}"' in html


def test_sections_are_independent():
    html = TEMPLATE.read_text(encoding="utf-8")
    for name in ("tomorrow", "history", "scores", "savings"):
        assert f'section("{name}"' in html
    assert "/*__DATA__*/null" in html


def test_test_winters_keep_their_label():
    """Regression: the 'kind' field from the data used to overwrite 'check', so test winters
    were drawn orange without '(test)'."""
    html = TEMPLATE.read_text(encoding="utf-8")
    assert 'Object.assign({}, r, {kind: "check"})' in html
