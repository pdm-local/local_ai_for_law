"""P0-4 regression: two documents with the same basename in different folders must stay distinct.

Real firms upload folder trees where `folderA/contract.pdf` and `folderB/contract.pdf` are different
documents. Document identity is therefore the group-relative path, never the bare basename. This is
enforced at two seams exercised here:
  * graph Document nodes are keyed on `source_path` (upsert_node), and
  * per-document processing state is keyed on `(group_id, doc_name)` where doc_name is the rel path.

Hermetic: temp RAG_DATA_DIR (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

from local_ai_law.services.graph import (
    upsert_node, get_node, set_doc_state, get_doc_state,
)

USER = "dupuser"
GID = "matter-beta"
REL_A = "folderA/contract.pdf"
REL_B = "folderB/contract.pdf"


def test_same_basename_different_folder_are_distinct_document_nodes():
    id_a = upsert_node(USER, "Document", "Contract", group_id=GID, source_path=REL_A)
    id_b = upsert_node(USER, "Document", "Contract", group_id=GID, source_path=REL_B)

    assert id_a != id_b, \
        "REGRESSION (P0-4): identical basenames in different folders collapsed to one document node"
    assert get_node(USER, id_a)["source_path"] == REL_A
    assert get_node(USER, id_b)["source_path"] == REL_B


def test_doc_state_is_keyed_by_relative_path_not_basename():
    # Mark only folderA's contract as graph-processed.
    set_doc_state(USER, GID, REL_A, graph_ok=True)

    a = get_doc_state(USER, GID, REL_A)
    b = get_doc_state(USER, GID, REL_B)  # same basename, never set

    assert a["graph_ok"] == 1, "precondition: folderA state should be set"
    assert b["graph_ok"] == 0, \
        "REGRESSION (P0-4): a same-basename doc in another folder shared processing state"
