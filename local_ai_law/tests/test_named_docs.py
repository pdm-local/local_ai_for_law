"""A question that names a document is answered from that document only.

`named_docs` matches a file name (with or without extension) standing alone in the
question, or a full title of at least four words. Hermetic: the summaries are stubbed.
"""
from __future__ import annotations

import pytest

from local_ai_law.services import retrieval, doc_summary

SUMMARIES = {
    "20240140-memo-005.md": {"title": "Juridisch advies inzake betalingsvordering"},
    "20240140-memo-018.md": {"title": "Analyse tegenstrijdige betalingstermijn opslagovereenkomst"},
    "map/huurovereenkomst.pdf": {"title": "Huur"},
}


@pytest.fixture(autouse=True)
def summaries(monkeypatch):
    monkeypatch.setattr(doc_summary, "load_doc_summaries", lambda g, u: SUMMARIES)


def named(q):
    return retrieval.named_docs(q, "g", "u")


def test_file_name_without_extension():
    assert named("Wat zegt doc_id: 20240140-memo-018 over de termijn?") == ["20240140-memo-018.md"]


def test_file_name_with_extension_and_folder():
    assert named("Lees huurovereenkomst.pdf even door") == ["map/huurovereenkomst.pdf"]


def test_a_plain_word_file_name_needs_its_extension():
    assert named("Wat is de huurovereenkomst?") == []


def test_a_longer_name_does_not_match_a_shorter_one():
    assert named("Zie 20240140-memo-01 en 20240140-memo-0185") == []


def test_full_title_names_the_document():
    q = "Wat staat er in Analyse tegenstrijdige betalingstermijn opslagovereenkomst?"
    assert named(q) == ["20240140-memo-018.md"]


def test_short_title_and_loose_words_name_nothing():
    assert named("Wat is de huur en de betalingsvordering?") == []


def test_two_named_documents():
    q = "Vergelijk 20240140-memo-005 met 20240140-memo-018."
    assert named(q) == ["20240140-memo-005.md", "20240140-memo-018.md"]


# ── The sibling guard in retrieve_carded ─────────────────────────────────────

from local_ai_law.services import doc_cards, embedding, cross_encoder  # noqa: E402

CARDS = ["20240140-memo-005.md", "20240140-brief-002.md", "20240140-memo-018.md",
         "20240150-memo-001.md"]
KINDS = {
    "20240140-memo-005.md": {"doc_type": "memo", "matter": "2024.0140", "title": "Advies"},
    "20240140-brief-002.md": {"doc_type": "brief", "matter": "2024.0140"},
    "20240140-memo-018.md": {"doc_type": "memo", "matter": "2024.0140", "title": "Analyse"},
    "20240150-memo-001.md": {"doc_type": "memo", "matter": "2024.0150"},
}


@pytest.fixture
def carded(monkeypatch):
    def fake_retrieve(query, col, top_k, embed_model, *, sources=None, vec=None):
        # The top pick wins every global pull: a sibling only shows when asked alone.
        pool = [s for s in CARDS if s in sources] if sources else CARDS
        return [{"source": s, "page": 1, "text": f"text of {s}", "score": 0.9}
                for s in sorted(pool, key=lambda s: s != CARDS[0])][:1 if len(pool) > 1 else top_k]

    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(doc_cards, "search_cards",
                        lambda *a, **k: [{"name": d, "card": d} for d in CARDS])
    monkeypatch.setattr(embedding, "embed_query", lambda q, m: [0.1])
    monkeypatch.setattr(doc_summary, "load_doc_summaries", lambda g, u: KINDS)
    monkeypatch.setattr(cross_encoder, "available", lambda: False)


def test_same_kind_same_matter_sibling_gets_a_chunk(carded):
    res = retrieval.retrieve_carded("welke beginselen noemt Hofman", None, "e", "u", "g",
                                    settings={"router_max_docs": 1})
    assert res["siblings"] == ["20240140-memo-005.md", "20240140-memo-018.md"]
    assert "20240140-memo-018.md" in {c["source"] for c in res["results"]}
    # Another kind, or another matter, is not a sibling.
    assert "20240140-brief-002.md" not in res["siblings"]
    assert "20240150-memo-001.md" not in res["siblings"]


def test_sibling_guard_off(carded):
    res = retrieval.retrieve_carded("welke beginselen noemt Hofman", None, "e", "u", "g",
                                    settings={"router_max_docs": 1, "sibling_max_docs": 0})
    assert res["siblings"] == []
    assert {c["source"] for c in res["results"]} == {"20240140-memo-005.md"}


def test_chunk_vote_adds_the_document_whose_words_match(monkeypatch):
    # Card ranking picks memo-005; the best chunk over the whole collection is memo-018's.
    def fake_retrieve(query, col, top_k, embed_model, *, sources=None, vec=None):
        if sources:
            return [{"source": s, "page": 1, "text": f"text of {s}", "score": 0.5}
                    for s in sources][:top_k]
        return [{"source": "20240140-memo-018.md", "page": 1, "text": "vangnet", "score": 0.8}]

    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(doc_cards, "search_cards",
                        lambda *a, **k: [{"name": "20240140-memo-005.md", "card": ""}])
    monkeypatch.setattr(embedding, "embed_query", lambda q, m: [0.1])
    monkeypatch.setattr(doc_summary, "load_doc_summaries", lambda g, u: KINDS)
    monkeypatch.setattr(cross_encoder, "available", lambda: False)
    res = retrieval.retrieve_carded("het vangnetbeginsel", None, "e", "u", "g", settings={})
    assert res["routed_docs"] == ["20240140-memo-005.md", "20240140-memo-018.md"]
    assert [c["source"] for c in res["results"]][-1] == "20240140-memo-018.md"
    off = retrieval.retrieve_carded("het vangnetbeginsel", None, "e", "u", "g",
                                    settings={"chunk_vote_docs": 0})
    assert off["routed_docs"] == ["20240140-memo-005.md"]


def test_matter_keeps_its_trailing_zero():
    s: dict = {}
    doc_summary._apply_frontmatter(s, "---\nmatter: 2024.0140\ndoc_type: memo\n---\nbody\n")
    assert s["matter"] == "2024.0140"
