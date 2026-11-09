"""P0-1 regression: a keyword-graph rebuild must NOT delete wiki pages.

The keyword-graph rebuild clears the graph via `delete_group_graph` (nodes + edges) on every
re-index. Wiki pages live in a separate registry and must survive that clear — wiping them here
would silently remove a matter's wiki from listing/search on a routine reindex (data loss).

Hermetic: temp RAG_DATA_DIR (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

from local_ai_law.services.graph import (
    upsert_node, upsert_edge, delete_group_graph,
    upsert_wiki_page, list_wiki_pages, get_node,
)

USER = "wikiuser"
GID = "matter-alpha"


def _seed_wiki_page():
    return upsert_wiki_page(USER, {
        "group_id": GID,
        "page_type": "concept",
        "slug": "indemnity-clause",
        "title": "Indemnity Clause",
        "file_path": "/tmp/does-not-need-to-exist.md",
        "body": "the indemnitor shall hold harmless the indemnitee",
    })


def test_keyword_graph_rebuild_preserves_wiki_pages():
    # Prior state: one graph node/edge AND one wiki page for the matter.
    doc_id = upsert_node(USER, "Document", "Some Contract", group_id=GID,
                         source_path="folderA/contract.pdf")
    kw_id = upsert_node(USER, "Concept", "indemnity", group_id=GID)
    upsert_edge(USER, doc_id, kw_id, "HAS_KEYWORD")
    pid = _seed_wiki_page()

    assert any(p["slug"] == "indemnity-clause" for p in list_wiki_pages(USER, GID)), \
        "precondition failed: wiki page was not registered"

    # The rebuild's clear step.
    delete_group_graph(USER, GID)

    # P0-1: the graph is gone, but every wiki page must remain.
    assert get_node(USER, doc_id) is None, "graph nodes should be cleared by the rebuild"
    pages = list_wiki_pages(USER, GID)
    assert any(p["id"] == pid or p["slug"] == "indemnity-clause" for p in pages), \
        "REGRESSION (P0-1): keyword-graph rebuild deleted a wiki page"
