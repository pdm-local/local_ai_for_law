"""A note is a .md file made through POST /api/groups/{id}/files: it lands in the client's
own folder and starts an index run like an upload, so chat can use it."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from local_ai_law.models import FileCreateIn
from local_ai_law.routes import documents


@pytest.fixture
def made(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(documents, "get_group", lambda g, u: {"id": g})
    monkeypatch.setattr(documents, "group_upload_dir", lambda g, u: tmp_path)
    monkeypatch.setattr(documents, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(documents, "_index_soon", lambda g, u: started.append((g, u)))
    return tmp_path, started


def _create(name, content="# Call\n\nThey agreed.\n"):
    body = FileCreateIn(name=name, content=content)
    return asyncio.run(documents.api_create_file("helix", body, username="demo"))


def test_note_is_saved_in_the_client_and_indexed(made):
    base, started = made
    out = _create("Call with Helix.md")
    assert out["rel_path"] == "Call with Helix.md"
    assert (base / "Call with Helix.md").read_text(encoding="utf-8").startswith("# Call")
    assert started == [("helix", "demo")]


def test_same_title_is_refused_not_overwritten(made):
    base, started = made
    _create("Call.md")
    with pytest.raises(HTTPException) as e:
        _create("Call.md", content="other")
    assert e.value.status_code == 409 and "already exists" in e.value.detail
    assert "They agreed" in (base / "Call.md").read_text(encoding="utf-8")
    assert len(started) == 1


def test_index_soon_skips_a_busy_run(monkeypatch):
    class St:
        is_indexing = True
    monkeypatch.setattr(documents, "idx_status", lambda g, u: St())
    called = []
    import local_ai_law.services.jobs as jobs
    monkeypatch.setattr(jobs, "submit_job", lambda *a, **k: called.append(a))
    documents._index_soon("helix", "demo")
    assert called == []
