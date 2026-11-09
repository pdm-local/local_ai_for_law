"""Per-file progress in the Files list: reading a file takes it to 50, embedding its own
pieces fills the rest, 100 means chat can use it (IdxStatus.file_progress).

Hermetic: extraction, chunking, the embedder, the collection and everything after the
embedding step are stubbed; no Ollama or Chroma needed.
"""
from __future__ import annotations

import pytest

from local_ai_law.services import indexer

PIECES = {"a.md": 3, "b.md": 1, "c.md": 0}


class FakeCol:
    def __init__(self):
        self.ids: list[str] = []

    def get(self):
        return {"ids": [], "metadatas": []}

    def delete(self, ids):
        pass

    def add(self, ids, embeddings, documents, metadatas):
        self.ids += ids


@pytest.fixture
def run(tmp_path, monkeypatch):
    for name in PIECES:
        (tmp_path / name).write_text("x")
    paths = sorted(tmp_path.iterdir())
    snaps: list[dict[str, int]] = []
    st = indexer.idx_status("g-progress", "u")

    def fake_embed(texts, model):
        with st._lock:
            snaps.append(dict(st.file_progress))
        return [[0.0] for _ in texts]

    def fake_split(raw, size, overlap):
        src = raw[0]["source"]
        return [{"source": src, "page": 1, "text": f"{src} piece {i}"} for i in range(PIECES[src])]

    monkeypatch.setattr(indexer, "get_group", lambda g, u: {"id": g})
    monkeypatch.setattr(indexer, "load_settings", lambda u: {"embed_batch_size": 2, "extraction_mode": "plain"})
    monkeypatch.setattr(indexer, "group_doc_paths", lambda g, grp, u: paths)
    monkeypatch.setattr(indexer, "group_upload_dir", lambda g, u: tmp_path)
    monkeypatch.setattr(indexer, "get_group_collection", lambda g, u, reset=False: FakeCol())
    monkeypatch.setattr(indexer, "load_doc_summaries", lambda g, u: {})
    monkeypatch.setattr(indexer, "file_version", lambda p: "v")
    monkeypatch.setattr(indexer, "extract_file", lambda p, table_format=None: [{"page": 1, "text": "x"}])
    monkeypatch.setattr(indexer, "split_chunks", fake_split)
    monkeypatch.setattr(indexer, "chunk_ok", lambda t: True)
    monkeypatch.setattr(indexer, "embed_texts", fake_embed)
    monkeypatch.setattr(indexer, "generate_group_overview", lambda *a, **k: None)
    monkeypatch.setattr(indexer, "summarize_all_docs", lambda *a, **k: None)
    indexer.do_index_group("g-progress", "u")
    return st, snaps


def test_reading_takes_each_file_to_half(run):
    st, snaps = run
    # Before the first batch is embedded: every file read, the empty one already done.
    assert snaps[0] == {"a.md": 50, "b.md": 50, "c.md": 100}


def test_embedding_fills_each_file_by_its_own_pieces(run):
    st, snaps = run
    # Batch 1 held two of a.md's three pieces: 50 + 50 * 2/3.
    assert snaps[1]["a.md"] == 83
    assert snaps[1]["b.md"] == 50


def test_every_file_ends_at_100_and_is_reported(run):
    st, snaps = run
    assert st.file_progress == {"a.md": 100, "b.md": 100, "c.md": 100}
    assert st.to_dict()["file_progress"] == st.file_progress
    assert not st.is_indexing


def test_file_list_shows_progress_only_while_indexing(tmp_path, monkeypatch):
    import asyncio
    from local_ai_law.routes import documents

    for name in ("ready.md", "busy.md", "waiting.md"):
        (tmp_path / name).write_text("x")
    st = indexer.IdxStatus()
    monkeypatch.setattr(documents, "get_group", lambda g, u: {"id": g})
    monkeypatch.setattr(documents, "group_upload_dir", lambda g, u: tmp_path)
    monkeypatch.setattr(documents, "load_group_manifest", lambda g, u: {})
    monkeypatch.setattr(documents, "group_chunk_stats", lambda g, u: {"ready.md": 4})
    monkeypatch.setattr(documents, "idx_status", lambda g, u: st)

    st.is_indexing = True
    st.file_progress = {"ready.md": 100, "busy.md": 60}
    tree = asyncio.run(documents.api_file_tree("g", "u"))
    got = {f["name"]: f.get("progress") for f in tree["files"]}
    assert tree["indexing"] is True
    assert got == {"ready.md": None, "busy.md": 60, "waiting.md": 0}

    st.is_indexing = False
    tree = asyncio.run(documents.api_file_tree("g", "u"))
    assert all("progress" not in f for f in tree["files"])
