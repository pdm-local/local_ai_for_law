"""Matter report: a firm's words are data, never HTML.

Document names and LLM summaries of a firm's documents go into the report. A crafted
document can steer its summary, so every value is escaped, and the PDF renderer (the
optional WeasyPrint extra) may not fetch any URL: an injected attachment link would
otherwise embed a local file in the PDF. Without WeasyPrint (the default build) the route
serves the same report as HTML.

Hermetic: RAG_DATA_DIR points at a temp tree (see conftest), no Ollama/GPU needed.
"""
from __future__ import annotations

import builtins
import json

import pytest
from starlette.testclient import TestClient

from local_ai_law.app import app
from local_ai_law.config import save_groups, user_overviews_dir, user_summaries_dir
from local_ai_law.services import reports
from local_ai_law.services.auth import create_token, register_user

U = "carol"
G = "g_report"
EVIL = ('<script>alert(1)</script><link rel="attachment" href="file:///nonexistent/secret">'
        '<img src="http://x.invalid/a">')


def _seed():
    user_overviews_dir(U).mkdir(parents=True, exist_ok=True)
    user_summaries_dir(U).mkdir(parents=True, exist_ok=True)
    (user_overviews_dir(U) / f"{G}.json").write_text(json.dumps({
        "summary": EVIL, "topics": [EVIL],
        "documents": [{"name": "a<b>.pdf", "description": EVIL}],
    }))
    (user_summaries_dir(U) / f"{G}.json").write_text(json.dumps({
        "a<b>.pdf": {"tldr": EVIL, "key_points": [EVIL]},
    }))


def test_report_html_escapes_every_value():
    _seed()
    html = reports.generate_report_html(U, G, "Matter <i>x</i>", "en")
    for tag in ("<script", "<link", "<img", "<i>", "<b>"):
        assert tag not in html, tag
    assert "&lt;script&gt;" in html
    assert "default-src 'none'" in html


@pytest.mark.parametrize("url", ["file:///nonexistent/secret", "http://x.invalid/a", "data:text/plain,hi"])
def test_pdf_renderer_fetches_nothing(url):
    with pytest.raises(ValueError):
        reports._no_fetch(url)


def test_route_serves_html_without_weasyprint(monkeypatch):
    _seed()
    try:
        register_user(U, "report-test-pass-1", role="user")
    except Exception:
        pass  # already registered by an earlier test in this temp tree
    save_groups([{"id": G, "name": "Matter one", "color": "cat-1"}], U)
    real_import = builtins.__import__

    def no_weasy(name, *a, **k):
        if name == "weasyprint" or name.startswith("weasyprint."):
            raise ImportError("not in this build")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_weasy)
    with TestClient(app) as c:
        r = c.get(f"/api/groups/{G}/report",
                  headers={"Authorization": f"Bearer {create_token(U, 'user')}"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert 'filename="Matter_one_report.html"' in r.headers["content-disposition"]
    assert "<script" not in r.text
