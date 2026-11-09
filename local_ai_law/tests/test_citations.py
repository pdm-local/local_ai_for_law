"""Answers keep their evidence (services/citations.py, ported from pdm_chat 2026-10-07).

A chat turn keeps each passage it was given word for word, and opening a citation later
says whether the file still says it: after a reindex, an edit or a deletion the passage
shown is still the one the answer stood on. Uses a real Chroma collection in the temp
data dir with made-up vectors, so no Ollama is needed.

Hermetic: temp RAG_DATA_DIR + RAG_USERS_FILE (see conftest).
"""
from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from local_ai_law.app import app
from local_ai_law.config import group_upload_dir, save_groups, user_history_dir
from local_ai_law.services import citations
from local_ai_law.services.auth import create_token, register_user
from local_ai_law.services.indexer import get_group_collection

USER, OTHER, GROUP, REL = "citeuser", "citeother", "matter1", "contracts/lease.txt"
PASSAGE = "[Doc: contracts/lease.txt | Page: 2]\n\nThe tenant pays rent on the first day."


def _index(col, text: str, version: str) -> None:
    """Stand in for the indexer: one passage of REL at this file version."""
    old = col.get(where={"source": REL})["ids"]
    if old:
        col.delete(ids=old)
    col.add(ids=[citations.sha(text)[:16]], embeddings=[[0.1, 0.2, 0.3]], documents=[text],
            metadatas=[{"source": REL, "page": 2, "chunk_index": 0, "file_hash": version, "heading_path": ""}])


@pytest.fixture()
def matter():
    save_groups([{"id": GROUP, "name": "Lease"}], USER)
    path = group_upload_dir(GROUP, USER) / REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("lease v1")
    col = get_group_collection(GROUP, USER, reset=True)
    _index(col, PASSAGE, citations.file_version(path))
    return path, col


def _save(sources: list[dict], session_id: str = "s1") -> None:
    d = user_history_dir(USER)
    (d / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id, "started": "now", "group_id": GROUP,
        "turns": [{"user": "q", "assistant": "a", "sources": sources}]}))


def _view(k: int = 0) -> dict:
    s = json.loads((user_history_dir(USER) / "s1.json").read_text())
    return citations.view(s, 0, k, USER, get_group_collection)


# ── storage ──────────────────────────────────────────────────────────────────

def test_record_keeps_each_passage_word_for_word(matter):
    path, col = matter
    kept = citations.record([
        {"type": "chunk", "source": REL, "page": 2, "score": 0.81, "text": PASSAGE},
        {"type": "wiki", "title": "Rent", "page_type": "concept", "score": 0.5, "text": "wiki text"},
    ], col)
    assert kept[0]["text"] == PASSAGE and kept[0]["sha"] == citations.sha(PASSAGE)
    assert kept[0]["version"] == citations.file_version(path)
    assert kept[0]["source"] == REL and kept[0]["page"] == 2 and not kept[0]["trimmed"]
    assert kept[1]["source"] == "Rent" and kept[1]["version"] == ""


def test_record_caps_a_passage_and_a_turn():
    long = "x" * (citations.EXCERPT + 500)
    kept = citations.record([{"source": f"f{i}", "text": long, "score": 0} for i in range(20)])
    assert all(len(p["text"]) <= citations.EXCERPT for p in kept)
    assert sum(len(p["text"]) for p in kept) <= citations.TURN_CAP
    assert kept[0]["trimmed"] and kept[-1]["text"] == ""
    assert kept[-1]["sha"] == citations.sha(long)  # the hash is of the whole passage


# ── states ───────────────────────────────────────────────────────────────────

def test_unchanged_file_is_kept(matter):
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], matter[1]))
    v = _view()
    assert v["state"] == "kept" and v["text"] == PASSAGE


def test_reindex_that_keeps_the_words_says_so(matter):
    path, col = matter
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], col))
    path.write_text("lease v1, longer now")
    os.utime(path, (1, 1))
    _index(col, PASSAGE, citations.file_version(path))
    assert _view()["state"] == "same"


def test_reindex_then_open_shows_the_old_passage_marked_changed(matter):
    path, col = matter
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], col))
    path.write_text("lease v2: rent is due on the fifth")
    os.utime(path, (2, 2))
    _index(col, "[Doc: contracts/lease.txt | Page: 2]\n\nRent is due on the fifth.", citations.file_version(path))
    v = _view()
    assert v["state"] == "changed" and v["text"] == PASSAGE
    assert "no longer holds" in v["note"]


def test_edit_without_a_reindex_is_changed(matter):
    path, col = matter
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], col))
    path.write_text("edited, not read again")
    os.utime(path, (3, 3))
    v = _view()
    assert v["state"] == "changed" and "not been read again" in v["note"]


def test_deleted_file_is_gone_but_the_passage_stays(matter):
    path, col = matter
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], col))
    path.unlink()
    v = _view()
    assert v["state"] == "gone" and v["text"] == PASSAGE


def test_a_walking_source_is_never_opened(matter):
    sources = citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], matter[1])
    sources[0]["source"] = "../../../../etc/passwd"
    _save(sources)
    assert _view()["state"] == "gone"


def test_an_answer_saved_before_this_change_still_opens(matter):
    _save([{"source": REL, "page": 2, "score": 0.7, "type": "chunk"}])
    assert _view()["state"] == "old"


def test_no_such_turn_or_passage():
    s = {"turns": [{"sources": []}]}
    with pytest.raises(LookupError):
        citations.view(s, 1, 0, USER, get_group_collection)
    with pytest.raises(LookupError):
        citations.view(s, 0, 0, USER, get_group_collection)


# ── the endpoint ─────────────────────────────────────────────────────────────

def test_endpoint_returns_the_passage_and_walls_off_other_users(matter):
    for u in (USER, OTHER):
        try:
            register_user(u, "Str0ng!pass99")
        except Exception:
            pass  # already there from an earlier test
    _save(citations.record([{"source": REL, "page": 2, "score": 1, "text": PASSAGE}], matter[1]))
    with TestClient(app) as c:
        mine = {"Authorization": f"Bearer {create_token(USER, 'user')}"}
        r = c.get("/api/sessions/s1/turns/0/passages/0", headers=mine)
        assert r.status_code == 200, r.text
        assert r.json()["text"] == PASSAGE and r.json()["state"] == "kept"
        assert c.get("/api/sessions/s1/turns/0/passages/9", headers=mine).status_code == 404
        theirs = {"Authorization": f"Bearer {create_token(OTHER, 'user')}"}
        c.cookies.clear()
        assert c.get("/api/sessions/s1/turns/0/passages/0", headers=theirs).status_code == 404
        c.cookies.clear()
        assert c.get("/api/sessions/s1/turns/0/passages/0").status_code == 401


# ── the matter list counts chunks off the event loop ─────────────────────────

def test_matter_list_counts_chunks_of_current_files_only(matter):
    from local_ai_law.routes.groups import _chunk_count, _list_groups
    path, col = matter
    col.add(ids=["stale"], embeddings=[[0.1, 0.2, 0.3]], documents=["old file"],
            metadatas=[{"source": "deleted.txt", "page": 1, "chunk_index": 0, "file_hash": "x", "heading_path": ""}])
    assert _chunk_count(col, [REL]) == 1
    assert _chunk_count(col, []) == 0
    g = next(g for g in _list_groups(USER) if g["id"] == GROUP)
    assert g["doc_count"] == 1 and g["chunk_count"] == 1
