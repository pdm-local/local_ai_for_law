"""Per-document keyword-card index for document-level retrieval.

The chunk index is great at "what does the corpus say about X" but poor at
"find THE document titled Y / involving party Z": near-identical sibling
documents in a matter produce near-identical chunks, so the router cannot tell
them apart. The fix is to retrieve at the *document* level first.

Each document gets one compact **card** (title, parties, type, focus, topics,
keywords) built by `doc_summary.build_card_text`. We embed the card once and
store it in a separate per-group Chroma collection. A question is then matched
against whole-document cards with a hybrid of vector similarity (paraphrase) and
BM25 over the card text (exact title / party / citation tokens), returning the
top-N candidate documents for the LLM router to pick from.

Generic across any corpus and language: every card field is LLM-extracted and
optional. Fail-open: an empty/absent card collection lets the caller fall back
to chunk-level routing.
"""

from __future__ import annotations

import chromadb

from local_ai_law.config import user_db_path, log
from local_ai_law.services.embedding import embed_texts, embed_query
from local_ai_law.services.doc_summary import load_doc_summaries, build_card_text
from local_ai_law.services.retrieval import _bm25_score, _tokenize


def cards_coll_name(group_id: str) -> str:
    return "crd_" + group_id.replace("-", "_")[:40]


def get_cards_collection(group_id: str, username: str = "default", reset: bool = False):
    client = chromadb.PersistentClient(path=str(user_db_path(username)))
    name = cards_coll_name(group_id)
    if reset:
        try:
            client.delete_collection(name)
        except Exception:
            log.debug("No existing card collection %s to delete on reset", name, exc_info=True)
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def build_doc_cards(group_id: str, username: str, embed_model: str) -> int:
    """(Re)build the card collection from the stored per-document summaries.

    Returns the number of cards written. One card per source document (the
    duplicate basename aliases stored by doc_summary are de-duped).
    """
    summaries = load_doc_summaries(group_id, username)
    items: list[tuple[str, str, str]] = []  # (source, card_text, title)
    seen: set[str] = set()
    for name, entry in summaries.items():
        if not isinstance(entry, dict):
            continue
        src = entry.get("doc_name") or name
        if src in seen:
            continue
        card = build_card_text(entry)
        if not card:
            continue
        seen.add(src)
        items.append((src, card, entry.get("title", "")))

    if not items:
        log.info("No doc cards to build for %s", group_id)
        try:
            get_cards_collection(group_id, username, reset=True)  # clear stale
        except Exception:
            log.debug("Could not clear stale card collection for %s", group_id, exc_info=True)
        return 0

    col = get_cards_collection(group_id, username, reset=True)
    BATCH = 32
    for i in range(0, len(items), BATCH):
        batch = items[i:i + BATCH]
        vecs = embed_texts([c for _, c, _ in batch], embed_model)
        if not vecs:
            continue
        col.add(
            ids=[s for s, _, _ in batch],
            embeddings=vecs,
            documents=[c for _, c, _ in batch],
            metadatas=[{"source": s, "title": t} for s, _, t in batch],
        )
    log.info("Built %d doc cards for %s", len(items), group_id)
    return len(items)


RRF_K = 60  # standard Reciprocal Rank Fusion damping constant (Cormack 2009)


def _parse_card_fields(card: str) -> dict:
    """Split a 'Key: value | Key: value' card string back into fields."""
    out: dict[str, str] = {}
    for part in card.split(" | "):
        key, sep, val = part.partition(": ")
        if sep:
            out[key.strip().lower()] = val.strip()
    return out


def _bm25_over(q_tokens, corpus_tokens, n):
    avg_dl = sum(len(t) for t in corpus_tokens) / max(n, 1)
    df: dict[str, int] = {}
    for toks in corpus_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    return [_bm25_score(q_tokens, corpus_tokens[i], avg_dl, n, df) for i in range(n)]


def _rrf_contrib(scores, *, all_matched=False):
    """RRF contribution per index for one ranked channel. Only documents the
    channel actually matched (score>0) earn credit, so an empty lexical channel
    (no token overlap) injects no noise. Dense similarity always matches."""
    n = len(scores)
    matched = list(range(n)) if all_matched else [i for i in range(n) if scores[i] > 1e-9]
    matched.sort(key=lambda i: scores[i], reverse=True)
    return {i: 1.0 / (RRF_K + pos) for pos, i in enumerate(matched)}


def search_cards(
    query: str,
    group_id: str,
    username: str,
    embed_model: str,
    top_n: int = 20,
    *,
    vec: list[float] | None = None,
) -> list[dict]:
    """Multi-channel document-card search fused with Reciprocal Rank Fusion.

    Channels (each an independent ranking, fused by RRF — scale-free, no weight
    tuning): dense card-vector similarity, BM25 over the full card, BM25 over the
    title field only, BM25 over the parties field only. The title/parties
    channels make "find THE document called X / involving Y" a first-class match
    even when the small embedder buries it. Returns up to `top_n` ranked
    {name, title, card, score}. Returns [] if no card index exists.
    """
    try:
        col = get_cards_collection(group_id, username)
        total = col.count()
    except Exception as e:
        log.debug("card search skipped: %s", e)
        return []
    if total == 0:
        return []

    if vec is None:
        vec = embed_query(query, embed_model)
        if not vec:
            return []

    res = col.query(query_embeddings=[vec], n_results=total)
    ids = res["ids"][0]
    docs = res["documents"][0]
    dists = res["distances"][0]
    metas = res["metadatas"][0]
    n = len(ids)

    fields = [_parse_card_fields(d) for d in docs]
    title_txt = [metas[i].get("title", "") or fields[i].get("title", "") for i in range(n)]
    party_txt = [fields[i].get("parties", "") for i in range(n)]

    q_tokens = _tokenize(query)
    dense = [max(0.0, 1.0 - dists[i]) for i in range(n)]
    full_b = _bm25_over(q_tokens, [_tokenize(d) for d in docs], n)
    title_b = _bm25_over(q_tokens, [_tokenize(t) for t in title_txt], n)
    party_b = _bm25_over(q_tokens, [_tokenize(t) for t in party_txt], n)

    contribs = [
        _rrf_contrib(dense, all_matched=True),
        _rrf_contrib(full_b),
        _rrf_contrib(title_b),
        _rrf_contrib(party_b),
    ]
    out: list[dict] = []
    for i in range(n):
        score = sum(c.get(i, 0.0) for c in contribs)
        out.append({
            "name": metas[i].get("source", ids[i]),
            "title": metas[i].get("title", ""),
            "card": docs[i],
            "score": round(score, 6),
        })
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_n]
