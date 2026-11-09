"""The Documents dialog: a chat limited to some documents only ever sees their chunks.

`retrieve_carded` and `retrieve_smart` take `sources` (the rel paths the user ticked).
Card ranking, widening and the wiki must all stay inside that set. Hermetic: the card
search, embedder and vector search are stubbed; no Ollama needed.
"""
from __future__ import annotations

import pytest

from local_ai_law.services import retrieval, doc_cards, embedding, doc_summary, cross_encoder

DOCS = ["a/lease.pdf", "b/invoice.pdf", "c/letter.md"]


@pytest.fixture
def calls(monkeypatch):
    seen: list[list[str] | None] = []

    def fake_retrieve(query, col, top_k, embed_model, *, sources=None, vec=None):
        seen.append(list(sources) if sources else None)
        pool = sources or DOCS
        return [{"source": s, "page": 1, "text": f"text of {s}", "score": 0.9} for s in pool][:top_k]

    def fake_cards(query, group_id, username, embed_model, top_n=50, vec=None):
        return [{"name": d, "card": d} for d in DOCS[:2]]

    monkeypatch.setattr(retrieval, "retrieve", fake_retrieve)
    monkeypatch.setattr(doc_cards, "search_cards", fake_cards)
    monkeypatch.setattr(embedding, "embed_query", lambda q, m: [0.1, 0.2])
    monkeypatch.setattr(doc_summary, "load_doc_summaries", lambda g, u: {})
    monkeypatch.setattr(cross_encoder, "available", lambda: False)
    return seen


def test_carded_picks_only_chosen_documents(calls):
    res = retrieval.retrieve_carded("what is the rent", None, "emb", "u", "g",
                                    settings={}, sources=["b/invoice.pdf"])
    assert res["routed_docs"] == ["b/invoice.pdf"]
    assert {c["source"] for c in res["results"]} == {"b/invoice.pdf"}
    assert all(s == ["b/invoice.pdf"] for s in calls)


def test_carded_falls_back_to_chosen_when_no_card_ranks(calls):
    # c/letter.md has no card in the ranked list: its chunks are fetched directly.
    res = retrieval.retrieve_carded("what is the rent", None, "emb", "u", "g",
                                    settings={}, sources=["c/letter.md"])
    assert {c["source"] for c in res["results"]} == {"c/letter.md"}
    assert calls == [["c/letter.md"]]


def test_carded_without_choice_uses_every_card(calls):
    # The card path alone: the chunk vote would add c/letter.md, the top global chunk.
    res = retrieval.retrieve_carded("what is the rent", None, "emb", "u", "g",
                                    settings={"chunk_vote_docs": 0})
    assert set(res["routed_docs"]) == set(DOCS[:2])


def test_smart_skips_the_wiki_when_documents_are_chosen(calls, monkeypatch):
    def no_wiki(*a, **k):
        raise AssertionError("wiki searched in a limited chat")
    monkeypatch.setattr(retrieval, "retrieve_wiki_first", no_wiki)
    res = retrieval.retrieve_smart("rent", None, 5, "emb", "u", "g",
                                   settings={}, sources=["a/lease.pdf"])
    assert {c["source"] for c in res["results"]} == {"a/lease.pdf"}
