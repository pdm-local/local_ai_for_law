"""
Wiki-first hybrid retrieval pipeline.

Retrieval order:
  1. FTS5 wiki search (SQLite BM25 over title/aliases/tags/body)
  2. Graph expansion on top wiki hit nodes (1-hop related pages)
  3. Vector + BM25 chunk retrieval (existing hybrid pipeline)
  4. Merge and rerank

Graceful fallback: wiki search returns 0 hits → skip to step 3.
Graph not populated → skip step 2.

Results are tagged with "type": "wiki" or "type": "chunk" so the
chat layer can label them clearly in the LLM context.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from pathlib import Path

from local_ai_law.services.embedding import embed_texts
from local_ai_law.services.chunking import chunk_ok
from local_ai_law.config import DATA_DIR, log


# ── Existing BM25 helpers (unchanged) ────────────────────────────────────────

_CODE_KEYWORDS = {
    "code", "implement", "implementation", "example", "function", "class",
    "script", "notebook", "ipynb", "python", "show", "sample", "snippet",
    "def ", "import", "syntax", "program", "algorithm",
}


def _bm25_score(
    query_tokens: list[str], doc_tokens: list[str],
    avg_dl: float, total_docs: int, df: dict[str, int],
    k1: float = 1.5, b: float = 0.75,
) -> float:
    score = 0.0
    dl = len(doc_tokens)
    doc_tf = Counter(doc_tokens)
    for qt in query_tokens:
        tf = doc_tf.get(qt, 0)
        if tf == 0:
            continue
        n = df.get(qt, 0)
        idf = math.log((total_docs - n + 0.5) / (n + 0.5) + 1.0)
        tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * dl / max(avg_dl, 1)))
        score += idf * tf_norm
    return score


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


# ── Pure chunk retrieval (the original retrieve() — preserved) ───────────────

def retrieve(query: str, col, top_k: int, embed_model: str, *,
             sources: list[str] | None = None, vec: list[float] | None = None) -> list[dict]:
    """
    Original BM25 + vector hybrid retrieval over ChromaDB chunks.
    Returns list of {text, source, page, score, type="chunk"}.

    `sources`: if given, restrict the vector search to chunks from those documents
    (Chroma `where` on the `source` metadata). `vec`: optional precomputed query
    embedding to avoid re-embedding.
    """
    if vec is None:
        vecs = embed_texts([query], embed_model)
        if not vecs:
            return []
        vec = vecs[0]
    total = col.count()
    if total == 0:
        return []

    q_lower = query.lower()
    is_code_query = any(kw in q_lower for kw in _CODE_KEYWORDS)

    # For code queries fetch extra results so code chunks have a chance to surface
    n_fetch = min(top_k * (6 if is_code_query else 4), total)
    where = {"source": {"$in": sources}} if sources else None
    sem_res = col.query(query_embeddings=[vec], n_results=n_fetch, where=where)

    docs_list  = sem_res["documents"][0]
    metas_list = sem_res["metadatas"][0]
    dists_list = sem_res["distances"][0]

    # Mark indices whose content contains a code fence (used in scoring below)
    code_ids: set[int] = set()
    if is_code_query:
        code_ids = {i for i, d in enumerate(docs_list) if "```" in d}

    query_tokens = _tokenize(query)
    all_doc_tokens = [_tokenize(d) for d in docs_list]
    avg_dl = sum(len(t) for t in all_doc_tokens) / max(len(all_doc_tokens), 1)
    df: dict[str, int] = {}
    for tokens in all_doc_tokens:
        for t in set(tokens):
            df[t] = df.get(t, 0) + 1

    # Compute all BM25 raw scores first so we can normalize against the batch max.
    raw_scores: list[float] = []
    valid_mask: list[bool] = []
    seen_content: set[str] = set()
    for i, (doc, _meta, _dist) in enumerate(zip(docs_list, metas_list, dists_list)):
        if not chunk_ok(doc):
            valid_mask.append(False)
            raw_scores.append(0.0)
            continue
        content_key = hashlib.md5(doc.encode("utf-8", errors="replace")).hexdigest()
        if content_key in seen_content:
            valid_mask.append(False)
            raw_scores.append(0.0)
            continue
        seen_content.add(content_key)
        valid_mask.append(True)
        raw_scores.append(_bm25_score(query_tokens, all_doc_tokens[i], avg_dl, len(docs_list), df))

    max_bm25 = max(raw_scores) if raw_scores else 1.0
    if max_bm25 < 1e-9:
        max_bm25 = 1.0

    results: list[dict] = []
    for i, (doc, meta, dist) in enumerate(zip(docs_list, metas_list, dists_list)):
        if not valid_mask[i]:
            continue

        vec_score  = max(0.0, 1.0 - dist)
        bm25_norm  = raw_scores[i] / max_bm25
        combined   = 0.70 * vec_score + 0.30 * bm25_norm
        if i in code_ids:
            combined = min(1.0, combined + 0.15)

        results.append({
            "type":   "chunk",
            "text":   doc,
            "source": meta["source"],
            "page":   meta["page"],
            "heading_path": meta.get("heading_path", ""),
            "score":  round(combined, 4),
        })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ── Wiki + graph retrieval ────────────────────────────────────────────────────

def _resolve_wiki_path(file_path: str) -> Path:
    p = Path(file_path)
    data_root = DATA_DIR.resolve()
    if p.exists():
        resolved = p.resolve()
        if resolved.is_relative_to(data_root):
            return p
        return None  # exists but escapes DATA_DIR — hard reject

    # Wiki metadata can contain absolute paths from an older app location.
    # Resolve the path suffix under the current DATA_DIR.
    parts = p.parts
    if "data" in parts:
        idx = parts.index("data")
        migrated = DATA_DIR.joinpath(*parts[idx + 1:])
        resolved = migrated.resolve()
        if resolved.is_relative_to(data_root) and migrated.exists():
            return migrated
    return None


def _load_wiki_page_text(file_path: str, max_chars: int = 3000) -> str:
    try:
        p = _resolve_wiki_path(file_path)
        if p is None or not p.exists():
            return ""
        raw = p.read_text(encoding="utf-8")
        # Strip frontmatter
        if raw.startswith("---"):
            end = raw.find("\n---", 4)
            if end != -1:
                raw = raw[end + 4:]
        return raw.strip()[:max_chars]
    except Exception:
        log.warning("Failed to load wiki page text", exc_info=True)
        return ""


def _wiki_hit_from_page(page: dict, score: float, *, max_chars: int = 3000) -> dict | None:
    text = _load_wiki_page_text(page.get("file_path", ""), max_chars=max_chars)
    if not text:
        return None
    return {
        "type":      "wiki",
        "title":     page.get("title", ""),
        "page_type": page.get("page_type", "WikiPage"),
        "slug":      page.get("slug", ""),
        "text":      text,
        "source":    page.get("title", ""),
        "page":      0,
        "score":     score,
        "file_path": str(_resolve_wiki_path(page.get("file_path", ""))),
        "snippet":   "",
        "sensitivity": page.get("sensitivity", "normal"),
    }


def _is_matter_overview_query(query: str) -> bool:
    if _lecture_number(query):
        return False
    q = query.lower()
    overview_terms = ("matter", "case", "file", "workspace", "documents")
    intent_terms = ("about", "overview", "summary", "summarize", "cover", "covers", "topics", "issues")
    return any(term in q for term in overview_terms) and any(term in q for term in intent_terms)


def _lecture_number(query: str) -> str | None:
    m = re.search(r"\b(?:lecture|class|lesson|week|session)\s*0*(\d{1,3})\b", query, re.I)
    return m.group(1) if m else None


def _compact_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _has_numbered_lesson_ref(text: str, number: str) -> bool:
    return bool(re.search(rf"\b(?:class|lecture|lesson|week|session)[\s_.-]*0*{re.escape(number)}\b", text, re.I))


def _direct_wiki_hits(
    query: str,
    username: str,
    group_id: str,
    sensitivity_max: str,
) -> list[dict]:
    try:
        from local_ai_law.services.graph import list_wiki_pages
    except ImportError:
        return []

    try:
        pages = list_wiki_pages(username, group_id, sensitivity_max=sensitivity_max)
    except Exception as e:
        log.warning("Direct wiki match skipped: %s", e, exc_info=True)
        return []

    direct: list[dict] = []

    if _is_matter_overview_query(query):
        for page in pages:
            tags = page.get("tags") or []
            if page.get("page_type") == "MatterOverview" or "matter-overview" in tags or "overview" in tags:
                hit = _wiki_hit_from_page(page, 1.0, max_chars=4500)
                if hit:
                    direct.append(hit)
                break

    lecture_no = _lecture_number(query)
    if lecture_no:
        matches: list[dict] = []
        for page in pages:
            haystack = " ".join([
                str(page.get("title", "")),
                str(page.get("slug", "")),
                " ".join(page.get("aliases") or []),
                " ".join(page.get("tags") or []),
            ])
            if _has_numbered_lesson_ref(haystack, lecture_no):
                matches.append(page)

        # Prefer the lecture summary page over associated notebook pages.
        matches.sort(key=lambda p: ("notebook" in _compact_key(p.get("slug", "")), p.get("title", "")))
        for page in matches[:3]:
            hit = _wiki_hit_from_page(page, 0.98, max_chars=3500)
            if hit:
                direct.append(hit)

    return direct


def retrieve_wiki(
    query: str,
    username: str,
    group_id: str,
    top_k: int = 5,
    sensitivity_max: str = "sensitive",
) -> list[dict]:
    """
    FTS5 wiki search + graph expansion.
    Returns list of {type="wiki", title, page_type, slug, text, score, file_path}.
    Returns [] if wiki is not populated.
    """
    try:
        from local_ai_law.services.graph import search_wiki_fts, expand_node
    except ImportError:
        return []

    # 1. FTS5 wiki search
    fts_hits = search_wiki_fts(
        username, group_id, query,
        limit=top_k * 2, sensitivity_max=sensitivity_max,
    )

    seen_ids: set[str] = set()
    wiki_results: list[dict] = []

    for hit in fts_hits:
        if hit["id"] in seen_ids:
            continue
        seen_ids.add(hit["id"])
        text = _load_wiki_page_text(hit["file_path"])
        if not text:
            continue
        wiki_results.append({
            "type":      "wiki",
            "title":     hit["title"],
            "page_type": hit["page_type"],
            "slug":      hit["slug"],
            "text":      text,
            "source":    hit["title"],
            "page":      0,
            "score":     round(min(1.0, max(0.0, -hit.get("score", 0) / 10 + 1.0)), 4),  # BM25 rank (~0–10) → 0–1
            "file_path": hit["file_path"],
            "snippet":   hit.get("snippet", ""),
            "sensitivity": hit.get("sensitivity", "normal"),
        })

    # 2. Graph expansion on top wiki hit
    if wiki_results:
        try:
            import hashlib
            import json
            node_id = hashlib.sha256(
                f"{username}:{group_id}:WikiPage:{fts_hits[0]['title']}".encode()
            ).hexdigest()[:24]
            related_nodes = expand_node(username, node_id, depth=1, max_nodes=10)
            for nd in related_nodes:
                if nd.get("type") != "WikiPage":
                    continue
                nd_meta = {}
                try:
                    nd_meta = json.loads(nd.get("metadata") or "{}")
                except Exception:
                    log.warning("Failed to parse node metadata", exc_info=True)
                    pass
                slug = nd_meta.get("slug", "")
                page_type = nd_meta.get("page_type", "")
                if not slug or not page_type:
                    continue
                # Use source_path stored on the node — avoids the wiki_pages vs nodes
                # ID-space mismatch (their IDs are derived differently and never match).
                fp = nd.get("source_path", "")
                if not fp:
                    continue
                if nd["id"] in seen_ids:
                    continue
                seen_ids.add(nd["id"])
                text = _load_wiki_page_text(fp)
                if text:
                    wiki_results.append({
                        "type":      "wiki",
                        "title":     nd["label"],
                        "page_type": page_type,
                        "slug":      slug,
                        "text":      text,
                        "source":    nd["label"],
                        "page":      0,
                        "score":     0.50,
                        "file_path": fp,
                        "snippet":   "",
                        "sensitivity": nd_meta.get("sensitivity", "normal"),
                    })
        except Exception as e:
            log.warning("Graph expansion skipped: %s", e, exc_info=True)

    wiki_results.sort(key=lambda x: x["score"], reverse=True)
    return wiki_results[:top_k]


# ── Merged wiki-first pipeline ────────────────────────────────────────────────

def retrieve_wiki_first(
    query: str,
    col,
    top_k: int,
    embed_model: str,
    username: str,
    group_id: str,
    wiki_top_k: int = 3,
    sensitivity_max: str = "sensitive",
) -> list[dict]:
    """
    Full wiki-first pipeline.
    Returns a merged, ranked list of wiki + chunk results.

    If wiki returns nothing, falls back gracefully to chunk-only retrieval.
    """
    from local_ai_law.config import load_settings
    settings = load_settings(username)
    wiki_boost = settings.get("wiki_boost", 1.1)

    # Step 1+2: direct wiki intent matches, then FTS wiki + graph.
    direct_hits = _direct_wiki_hits(query, username, group_id, sensitivity_max)
    wiki_hits = direct_hits + retrieve_wiki(
        query, username, group_id,
        top_k=wiki_top_k + len(direct_hits),
        sensitivity_max=sensitivity_max,
    )

    # Step 3: chunk retrieval
    chunk_hits = retrieve(query, col, top_k, embed_model)

    if not wiki_hits:
        return chunk_hits  # pure fallback

    # Step 4: merge — wiki results get a score boost to prioritize synthesis
    merged: list[dict] = []
    for hit in wiki_hits:
        merged.append({**hit, "score": min(1.0, hit["score"] * wiki_boost)})
    merged.extend(chunk_hits)

    # Deduplicate by content key
    seen: set[str] = set()
    out: list[dict] = []
    for item in merged:
        key = item.get("slug") or item.get("text", "")[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(item)

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:top_k + wiki_top_k]


# ── Smart retrieval: optional query expansion + second pass on weak results ──

# Combined score = 0.70*vec + 0.30*bm25_norm; the batch-max BM25 hit contributes
# ~0.30 by construction, so a top score below 0.45 means vector similarity was
# near zero — no real semantic match. The "<3 above 0.35" arm catches comparison
# questions where only one of several needed documents surfaced.
_SECOND_PASS_MIN_TOP   = 0.45
_SECOND_PASS_MIN_COUNT = 3
_SECOND_PASS_FLOOR     = 0.35


def _results_weak(results: list[dict]) -> bool:
    chunks = [r for r in results if r.get("type") == "chunk"]
    if not chunks:
        return True
    if max(c["score"] for c in chunks) < _SECOND_PASS_MIN_TOP:
        return True
    strong = sum(1 for c in chunks if c["score"] >= _SECOND_PASS_FLOOR)
    return strong < _SECOND_PASS_MIN_COUNT


def _merge_results(first: list[dict], second: list[dict], cap: int) -> list[dict]:
    """Union of both passes, deduped by content key, keeping the higher score."""
    best: dict[str, dict] = {}
    for item in first + second:
        key = item.get("slug") or item.get("text", "")[:120]
        if key not in best or item["score"] > best[key]["score"]:
            best[key] = item
    out = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return out[:cap]


def retrieve_smart(
    query: str,
    col,
    top_k: int,
    embed_model: str,
    username: str,
    group_id: str,
    *,
    wiki_top_k: int = 3,
    sensitivity_max: str = "sensitive",
    use_wiki: bool = True,
    settings: dict | None = None,
    sources: list[str] | None = None,
) -> dict:
    """
    Retrieval with optional query expansion and a second pass when the first
    one is weak. Wraps retrieve()/retrieve_wiki_first() unchanged.

    `sources`: the documents the user chose for this chat. When given, only their
    chunks are searched and the wiki is skipped (a wiki page spans every document).

    Returns {"results": [...], "expanded": bool, "second_pass": bool,
             "query_used": str}.
    """
    from local_ai_law.services.query_expansion import expand_query, should_expand

    settings = settings or {}
    expanded = False
    q = query

    if should_expand(query, settings.get("query_expansion", "off")):
        q2 = expand_query(query, settings)
        if q2 != query:
            q, expanded = q2, True

    def _pass(search_query: str) -> list[dict]:
        if sources:
            return retrieve(search_query, col, top_k, embed_model, sources=sources)
        if use_wiki:
            return retrieve_wiki_first(
                search_query, col, top_k, embed_model, username, group_id,
                wiki_top_k=wiki_top_k, sensitivity_max=sensitivity_max,
            )
        return retrieve(search_query, col, top_k, embed_model)

    results = _pass(q)
    second_pass = False

    if settings.get("retrieval_second_pass") and _results_weak(results):
        # Reformulate from the ORIGINAL query (forced expansion) so a bad
        # first expansion can't poison the retry.
        q2 = expand_query(query, settings)
        if q2 != q:
            second = _pass(q2)
            if second:
                cap = top_k + (wiki_top_k if use_wiki else 0)
                results = _merge_results(results, second, cap)
                second_pass = True
                q = q2

    return {"results": results, "expanded": expanded,
            "second_pass": second_pass, "query_used": q}


import threading

_backfill_lock = threading.Lock()
_backfill_inflight: set[str] = set()


def _maybe_backfill_summaries(username: str, group_id: str, col) -> None:
    """Submit a non-blocking LOW job to generate missing doc summaries.

    The router routes on each doc's summary; docs without one route on a snippet
    alone. Generate them in the background (deduped per group) so the next query
    routes on real keys. Fail-open — never blocks or breaks the current query.
    """
    key = f"{username}:{group_id}"
    with _backfill_lock:
        if key in _backfill_inflight:
            return
        _backfill_inflight.add(key)
    try:
        from local_ai_law.config import (
            DEFAULT_SETTINGS, load_settings, get_group, group_doc_paths,
        )
        from local_ai_law.services.jobs import create_job, submit_job
        from local_ai_law.services.doc_summary import summarize_all_docs

        group = get_group(group_id, username)
        if not group:
            with _backfill_lock:
                _backfill_inflight.discard(key)
            return
        paths = group_doc_paths(group_id, group, username)
        cfg = load_settings(username)

        def _worker(ctx):
            try:
                summarize_all_docs(
                    group_id, username, paths, col,
                    model=cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"]),
                    force=False,
                )
            finally:
                with _backfill_lock:
                    _backfill_inflight.discard(key)
            return {"backfilled": True}

        job_id = create_job("doc_summaries", username, group_id)
        submit_job(job_id, _worker, priority="LOW")
        log.info("Queued doc-summary backfill for %s", key)
    except Exception as e:
        with _backfill_lock:
            _backfill_inflight.discard(key)
        log.warning("summary backfill submit failed for %s: %s", group_id, e)


def _doc_summary_text(entry: dict | None) -> str:
    """Compact routing key from a doc_summaries entry: tl;dr + topics."""
    if not entry:
        return ""
    tldr = (entry.get("tldr") or "").strip()
    topics = entry.get("topics") or []
    parts = []
    if tldr:
        parts.append(tldr)
    if topics:
        parts.append("topics: " + ", ".join(str(t) for t in topics[:8]))
    return " ".join(parts)


def _card_ce_text(c: dict) -> str:
    """Render a candidate document for the cross-encoder. Identifying signals
    (title, type, parties) first, then the distinctive `focus`/summary — which is
    what separates sibling docs with near-identical titles. No field is repeated
    (the old version re-appended the whole card, crowding out the focus). Capped
    for the 512-token cross-encoder window."""
    bits = [f"Title: {c.get('title','')}"]
    dt = (c.get("doc_type") or "").strip()
    if dt:
        bits.append(f"Type: {dt}")
    parties = ", ".join(str(x) for x in (c.get("parties") or [])[:8])
    if parties:
        bits.append(f"Parties: {parties}")
    if c.get("privileged"):
        bits.append("Privileged: yes")
    focus = (c.get("summary") or c.get("snippet") or "").strip()
    if focus:
        bits.append(focus)
    return " | ".join(bits)[:900]


def retrieve_routed(
    query: str,
    col,
    embed_model: str,
    username: str,
    group_id: str,
    settings: dict | None = None,
) -> dict:
    """Document-routed retrieval.

    Narrow to candidate documents by vector similarity, let a bounded LLM pick the
    most relevant one(s), then fetch a small number of chunks ONLY from the picked
    documents. Returns the same shape as retrieve_smart() so chat.py can swap in.
    Extra key: "routed_docs" = the document names that were used.
    """
    from local_ai_law.services.doc_router import route_documents
    from local_ai_law.services.doc_summary import load_doc_summaries
    from local_ai_law.services.reranker import rerank

    settings = settings or {}
    max_chunks = int(settings.get("router_max_chunks", 5))
    max_docs = int(settings.get("router_max_docs", 3))
    n_candidates = int(settings.get("router_candidates", 12))

    empty = {"results": [], "expanded": False, "second_pass": False,
             "query_used": query, "routed_docs": []}

    vecs = embed_texts([query], embed_model)
    if not vecs:
        return empty
    vec = vecs[0]
    total = col.count()
    if total == 0:
        return empty

    # 1. Build the candidate pool with the hybrid BM25 + vector retriever, then
    #    aggregate by source (best chunk per document). BM25 is what surfaces the
    #    right document for keyword-y questions (e.g. "Petitum") that the embedding
    #    model alone scores poorly.
    pool = retrieve(query, col, max(n_candidates * 4, 40), embed_model, vec=vec)
    best: dict[str, dict] = {}
    for r in pool:
        src = r["source"]
        if src not in best or r["score"] > best[src]["score"]:
            best[src] = {"score": r["score"], "snippet": r["text"]}
    if not best:
        return empty

    ranked = sorted(best.items(), key=lambda kv: kv[1]["score"], reverse=True)[:n_candidates]

    # 2. Attach summaries (where available) and route.
    summaries = load_doc_summaries(group_id, username)
    candidates = [
        {"name": src,
         "snippet": info["snippet"],
         "summary": _doc_summary_text(summaries.get(src))}
        for src, info in ranked
    ]

    # Guarantee router keys: if many candidates lack a summary, backfill them in
    # the background (non-blocking) so the next query routes on real summaries.
    missing = [c for c in candidates if not c["summary"]]
    if missing and len(missing) >= max(2, len(candidates) // 2):
        _maybe_backfill_summaries(username, group_id, col)

    # Rerank the candidate pool (one bounded LLM pass) so the router sees the
    # strongest candidates first. Fail-open: returns input order on any error.
    if settings.get("reranker", True):
        candidates = rerank(query, candidates, settings)

    picked = route_documents(query, candidates, settings, max_docs=max_docs)

    # 3. Fallback: router unsure → use the single best (reranked) candidate.
    if not picked:
        picked = [candidates[0]["name"]]

    # 4. Fetch ≤ max_chunks chunks ONLY from the picked documents (reuse query vec).
    chunks = retrieve(query, col, max_chunks, embed_model, sources=picked, vec=vec)

    return {"results": chunks, "expanded": False, "second_pass": False,
            "query_used": query, "routed_docs": picked}


def _kw_norm(k: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (k or "").lower())


# Matter token in a doc name: "lawfox-0109-memo-002" -> "0109", "2025.0104-..." -> "2025.0104".
# The 3-4 digit dossier segment between dashes is the matter; a leading YYYY.NNNN also works.
_MATTER_NAME_RE = re.compile(r"\b(\d{4}[.\-]\d{3,4})\b|-(\d{3,4})-")


def _matter_norm(m: str) -> str:
    """Canonical matter id. The frontmatter carries the full `2025.0117` while the doc
    NAME carries only `0117`, so the two forms must collapse or sibling docs (one read
    from frontmatter, one name-parsed) would land in different matters and never group.
    Drop a leading 4-digit-year prefix → both become `0117`. Within one corpus/firm-year
    this is unambiguous (dossier numbers are year-scoped)."""
    m = (m or "").strip()
    return re.sub(r"^\d{4}[.\-]", "", m)


def _matter_of(name: str, entry: dict | None) -> str:
    """Matter (dossier) id for a document — the join key for matter-anchored retrieval.

    Prefer the frontmatter `matter` carried on the card (`_apply_frontmatter` stores it);
    fall back to parsing the matter token from the doc NAME so this works on summaries
    built before the matter field existed (zero-migration). Both paths run through
    `_matter_norm` so frontmatter (`2025.0117`) and name-parse (`0117`) agree. Returns ""
    when no matter is derivable (e.g. a corpus without the lawfox/YYYY.NNNN naming) —
    callers treat "" as "no matter signal" and skip anchoring, never grouping unrelated
    docs under a blank id."""
    m = (entry or {}).get("matter")
    if m and str(m).strip():
        return _matter_norm(str(m))
    hit = _MATTER_NAME_RE.search(name or "")
    if hit:
        return _matter_norm(hit.group(1) or hit.group(2) or "")
    return ""


def _title_norm(t: str) -> str:
    return " ".join(re.findall(r"\w+", (t or "").lower()))


def named_docs(query: str, group_id: str, username: str) -> list[str]:
    """The documents a question names outright: by file name, with or without its
    extension ("20240140-memo-018"), or by its full title (at least four words).
    The chat answers such a question from those documents only, because a dossier holds
    siblings (same author, parties, statute and subject) that card ranking cannot tell
    apart, and the wrong sibling can say the opposite. A name must stand alone in the
    question, so "memo-01" never matches "memo-018". [] when nothing is named."""
    from local_ai_law.services.doc_summary import load_doc_summaries
    q = (query or "").lower()
    qt = f" {_title_norm(query)} "
    out: list[str] = []
    for name, e in load_doc_summaries(group_id, username).items():
        stem = Path(name).stem.lower()
        # A plain-word stem ("contract") is an ordinary word in a question: it names
        # the file only with its extension.
        keys = {Path(name).name.lower()} | ({stem} if re.search(r"[\d_.-]", stem) else set())
        hit = any(len(k) >= 6 and re.search(rf"(?<![\w-]){re.escape(k)}(?![\w-])", q)
                  for k in keys)
        if not hit and isinstance(e, dict):
            t = _title_norm(e.get("title", ""))
            hit = len(t.split()) >= 4 and f" {t} " in qt
        if hit:
            out.append(name)
    return sorted(out)


def _matter_siblings(picked: list[str], summaries: dict, anchors: set[str],
                     cap: int) -> list[str]:
    """Every document in the `anchors` matters that isn't already picked, capped at `cap`
    docs PER matter (counting those already picked). This is the matter-anchored expansion:
    instead of betting on which same-matter sibling the card router guessed, pull the whole
    dossier so the per-doc chunk floor can surface the right facts. Deterministic; bounded
    so a large matter can't blow the context."""
    have: dict[str, int] = {}
    for name in picked:
        m = _matter_of(name, summaries.get(name))
        if m in anchors:
            have[m] = have.get(m, 0) + 1
    out: list[str] = []
    for name, e in summaries.items():
        if name in picked or not isinstance(e, dict):
            continue
        m = _matter_of(name, e)
        if m and m in anchors and have.get(m, 0) < cap:
            out.append(name)
            have[m] = have.get(m, 0) + 1
    return out


def _keyword_expand(picked: list[str], summaries: dict, max_add: int,
                    top_n: int = 2) -> list[str]:
    """1-hop keyword-graph expansion for multi-doc (connection) questions: return up
    to `max_add` document names (not already in `picked`) linked to the TOP-N picked
    docs by a rare *connective* keyword. Mirrors services/wiki.py:build_keyword_graph
    — a keyword shared by few docs (low document-frequency, df>1) is a strong link;
    generic hubs (df > half the corpus) and singletons (df==1) are ignored.

    A sibling is scored by (a) how many of the top-N picks link to it — a doc that
    bridges two picks is the strongest connection candidate — then (b) the rarity of
    the linking keyword(s). Deterministic; expanding off the top-N (not just top-1)
    is what lets the *second* gold doc of a connection pair surface."""
    if not picked or max_add <= 0:
        return []
    # Document frequency of every normalized keyword across the group.
    df: dict[str, int] = {}
    kw_by_doc: dict[str, set[str]] = {}
    for name, e in summaries.items():
        if not isinstance(e, dict):
            continue
        ks = {_kw_norm(k) for k in (e.get("keywords") or []) if _kw_norm(k)}
        kw_by_doc[name] = ks
        for k in ks:
            df[k] = df.get(k, 0) + 1
    n = max(2, len(kw_by_doc))
    df_cap = max(2, n // 2)               # drop generic hubs (in > half the corpus)
    chosen = set(picked)
    bridges: dict[str, set] = {}        # sib -> set of picks linking to it
    rarity: dict[str, float] = {}       # sib -> summed link rarity (1/df)
    for src in picked[:max(1, top_n)]:
        cand_kw = sorted((k for k in kw_by_doc.get(src, set()) if 1 < df.get(k, 0) <= df_cap),
                         key=lambda k: df[k])
        seen_kw: set = set()
        for k in cand_kw:
            for name, ks in kw_by_doc.items():
                if name in chosen or k not in ks:
                    continue
                bridges.setdefault(name, set()).add(src)
                if (name, k) not in seen_kw:        # count each keyword's rarity once
                    rarity[name] = rarity.get(name, 0.0) + 1.0 / df[k]
                    seen_kw.add((name, k))
    # bridges most distinct picks first, then rarest link, then stable name order.
    ranked = sorted(bridges, key=lambda s: (-len(bridges[s]), -rarity[s], s))
    return ranked[:max_add]


def retrieve_carded(
    query: str,
    col,
    embed_model: str,
    username: str,
    group_id: str,
    settings: dict | None = None,
    sources: list[str] | None = None,
) -> dict:
    """Document-card retrieval: search whole-document cards (hybrid vector+BM25)
    for the top-N candidate documents, fuse CE+RRF to pick the best few, then fetch
    chunks from the picked documents.

    This resolves "find the document titled/involving X" questions that chunk
    soup cannot — title, parties and type are first-class card fields. Same return
    shape as retrieve_routed(). Fail-open: if no card index exists yet, falls back
    to chunk-level retrieve_routed().

    For multi-doc reasoning questions (`is_multidoc`), doc SELECTION is widened before
    chunks are fetched (step 3b) because card ranking can't tell same-matter siblings
    apart: (a) `matter_anchor` pulls the whole matter of the top picks (≤`matter_max_docs`
    per matter); (b) `entity_join` adds cross-matter docs sharing a party/statute named in
    the query (≤`entity_max_docs`). The per-doc floor (`router_per_doc_floor`) then
    guarantees ≥1 chunk from every routed doc. `_keyword_expand` remains only as the
    fallback when neither matter nor entity yields a second doc. Single-doc questions skip
    all of this; they get the sibling guard instead (step 5b): up to `sibling_max_docs`
    same-kind, same-matter documents ranked near the top pick get one appended chunk each
    and come back as `siblings`, which the chat names to the model as possibly disagreeing.
    Every question also gets the chunk vote (step 5a): the documents behind the top
    chunks over the whole collection join the picks (`chunk_vote_docs`, one appended
    chunk each), because a card can miss the document whose exact wording is asked about.
    Returns `doc_summaries` for the routed docs (answer-context injection).

    `sources`: the documents the user chose for this chat. Every pick, widening and
    expansion stays inside them; when no card of theirs ranks (or there is no card
    index) the chunks come straight from the chosen documents.
    """
    from local_ai_law.services.doc_cards import search_cards, RRF_K
    from local_ai_law.services.doc_summary import load_doc_summaries
    from local_ai_law.services import cross_encoder
    from local_ai_law.services.embedding import embed_query

    from local_ai_law.services.answer_templates import is_multidoc

    settings = settings or {}
    # Cross-encoder ranks cheaply, so feed it a wide pool (no lost-in-the-middle).
    top_n = int(settings.get("card_candidates", 50))
    max_docs = int(settings.get("router_max_docs", 5))
    max_chunks = int(settings.get("router_max_chunks", 5))
    n_expand = int(settings.get("card_expand", 0))

    # Multi-doc reasoning (compare / connection / contradiction): widen breadth so every
    # comparand surfaces, force a per-doc chunk floor (below) so a routed doc can't end
    # up with 0 chunks, and bump the keyword-graph expansion. Single-doc questions keep
    # the precise, narrow path (no dilution, no extra Chroma queries).
    multidoc = is_multidoc(query)
    per_doc_floor = 0
    if multidoc:
        max_docs = max(max_docs, int(settings.get("multidoc_max_docs", 5)))
        max_chunks = max(max_chunks, max_docs + 2)
        n_expand = max(n_expand, 3)
        per_doc_floor = int(settings.get("router_per_doc_floor", 1))

    empty = {"results": [], "expanded": False, "second_pass": False,
             "query_used": query, "routed_docs": []}

    vec = embed_query(query, embed_model)
    if not vec:
        return empty

    # 1. Hybrid card search → top-N candidate documents.
    cards = search_cards(query, group_id, username, embed_model, top_n=top_n, vec=vec)
    allowed = set(sources) if sources else None
    if allowed is not None:
        cards = [c for c in cards if c["name"] in allowed]
        if not cards:
            chosen = sorted(allowed)
            return {**empty, "results": retrieve(query, col, max_chunks, embed_model,
                                                  sources=chosen, vec=vec),
                    "routed_docs": chosen[:max_docs]}
    if not cards:
        # No card index (older group) → fall back to chunk-level routing.
        return retrieve_routed(query, col, embed_model, username, group_id, settings)

    # 2. Build rich candidates (title + parties + focus).
    summaries = load_doc_summaries(group_id, username)
    candidates = []
    for c in cards:
        entry = summaries.get(c["name"]) or {}
        candidates.append({
            "name": c["name"],
            "title": c.get("title") or entry.get("title", ""),
            "doc_type": entry.get("doc_type", ""),
            "parties": entry.get("parties") or [],
            "privileged": entry.get("privileged"),
            # focus is the per-document discriminator; fall back to tldr+topics.
            "summary": (entry.get("focus") or "").strip() or _doc_summary_text(entry),
            "snippet": c.get("card", ""),
        })

    # 3. Pick the best documents by FUSING the RRF and cross-encoder rankings
    #    (reciprocal-rank fusion, equal weight). CE alone is a pure semantic
    #    reranker that sometimes demotes an exact-title match the question named;
    #    keeping the RRF signal fixes that (measured: statutes recall 0.83->0.97 EN,
    #    no type regressed). `candidates` is already in RRF order, so the candidate
    #    index IS the RRF rank. Fail-open to plain RRF order (which beats the LLM
    #    router) if the cross-encoder is unavailable.
    w = float(settings.get("ce_fuse_weight", 0.5))
    picked = []
    if cross_encoder.available():
        try:
            ce = cross_encoder.score(query, candidates, _card_ce_text)
            order = sorted(range(len(candidates)), key=lambda i: ce[i], reverse=True)
            ce_rank = {idx: pos for pos, idx in enumerate(order)}
            fused = sorted(range(len(candidates)), key=lambda i: (
                w * (1.0 / (RRF_K + ce_rank[i])) + (1.0 - w) * (1.0 / (RRF_K + i))),
                reverse=True)
            picked = [candidates[i]["name"] for i in fused[:max_docs]]
        except Exception:
            picked = []
    if not picked:
        picked = [c["name"] for c in candidates[:max_docs]]

    # 3b. Multi-doc breadth (compare / connection / contradiction): the gold is a SET of
    #     documents, but card ranking can't tell same-matter siblings apart, so the router
    #     picks the wrong sibling and its chunk is unreachable. Attack doc SELECTION (the
    #     real bottleneck) BEFORE fetching chunks: (a) matter-anchor — pull the WHOLE matter
    #     of the top picks; (b) entity-join — for cross-matter connections, add docs sharing
    #     a party/statute named in the query. The per-doc floor (step 4) then guarantees a
    #     chunk from every added doc. Single-doc path skips all of this (precise + fast).
    n_picked0 = len(picked)
    if multidoc and picked:
        added: list[str] = []
        if settings.get("matter_anchor", True):
            anchors = {_matter_of(picked[i], summaries.get(picked[i]))
                       for i in range(min(2, len(picked)))}
            anchors.discard("")
            if anchors:
                added += _matter_siblings(picked, summaries, anchors,
                                          int(settings.get("matter_max_docs", 8)))
        if settings.get("entity_join", True):
            from local_ai_law.services.entity_index import load_entity_index, join_docs
            ejoin = join_docs(query, load_entity_index(group_id, username),
                              int(settings.get("entity_max_docs", 6)),
                              exclude=set(picked) | set(added))
            added += ejoin
        if allowed is not None:
            added = [d for d in added if d in allowed]
        if added:
            picked = picked + added
            # Widen the chunk budget so the per-doc floor can represent every routed doc.
            max_chunks = max(max_chunks, len(picked) + 2)

    # 4. Fetch ≤ max_chunks chunks from the picked documents (reuse query vec). For
    #    multi-doc questions guarantee ≥ per_doc_floor chunks from EACH picked doc, then
    #    fill the rest by global score — so a doc that was routed in is always represented
    #    (the gap that left compare/connection answers seeing only one of several docs).
    chunks = _retrieve_picked(query, col, embed_model, picked, max_chunks,
                              per_doc_floor, vec)

    # 5. Keyword-graph fallback: ONLY when matter-anchor + entity-join added nothing (e.g.
    #    an ambiguous "het dossier" with no matter cue and no named entity). Append docs
    #    sharing the top pick's rarest keyword + a few extra chunks. Primary chunks untouched.
    expanded = len(picked) > n_picked0
    if n_expand > 0 and picked and not expanded:
        extra_docs = _keyword_expand(picked, summaries, n_expand)
        if allowed is not None:
            extra_docs = [d for d in extra_docs if d in allowed]
        if extra_docs:
            n_chunks = int(settings.get("expand_chunks", 2)) * len(extra_docs)
            extra = retrieve(query, col, n_chunks, embed_model, sources=extra_docs, vec=vec)
            seen = {(c.get("source"), c.get("page"), (c.get("text") or "")[:120]) for c in chunks}
            for c in extra:
                key = (c.get("source"), c.get("page"), (c.get("text") or "")[:120])
                if key not in seen:
                    chunks.append(c); seen.add(key)
            picked = picked + extra_docs
            expanded = True

    # 5a. Chunk vote: cards are short summaries, so in a dossier of near-identical
    #     documents (thirty on one dispute) the one whose WORDING the question uses can
    #     rank 33rd by card while its chunk ranks 2nd over the whole collection. The
    #     documents behind the top global chunks join `picked` (up to `chunk_vote_docs`),
    #     each with its best chunk appended; the primary chunks are untouched.
    n_vote = int(settings.get("chunk_vote_docs", 2))
    if n_vote > 0 and picked:
        voted: list[str] = []
        # A wide pull: BM25 only re-ranks the vector pool (4 x top_k), and a document
        # found by its exact words, not its meaning, sits outside a narrow one.
        for c in retrieve(query, col, max(12, n_vote * 4), embed_model,
                          sources=sorted(allowed) if allowed else None, vec=vec):
            s = c.get("source")
            if s and s not in picked and s not in voted and len(voted) < n_vote:
                voted.append(s)
                chunks.append(c)
        picked = picked + voted

    # 5b. Sibling guard (single-doc path): a dossier often holds several documents of the
    #     same kind on the same point (two memos, same author, parties and statute) that
    #     card ranking cannot tell apart, and they can say the opposite. Same doc_type and
    #     same matter as the top pick, among the top-ranked candidates: each gets one
    #     chunk of its own, appended (the primary chunks are untouched), and they are
    #     returned as `siblings` so the answer step is told to pick the one the question
    #     points to, or to say how they differ. Bounded by `sibling_max_docs`.
    siblings: list[str] = []
    n_sib = int(settings.get("sibling_max_docs", 2))
    if not multidoc and picked and n_sib > 0:
        top = summaries.get(picked[0]) or {}
        t_type = (top.get("doc_type") or "").strip().lower()
        t_matter = _matter_of(picked[0], top)
        ranked = list(dict.fromkeys(picked + [c["name"] for c in candidates]))[:max_docs + 3]
        for name in ranked[1:]:
            if len(siblings) >= n_sib:
                break
            e = summaries.get(name) or {}
            if (t_type and (e.get("doc_type") or "").strip().lower() == t_type
                    and _matter_of(name, e) == t_matter):
                siblings.append(name)
        have = {c.get("source") for c in chunks}
        for name in siblings:
            if name not in have:
                chunks += retrieve(query, col, 1, embed_model, sources=[name], vec=vec)
            if name not in picked:
                picked.append(name)
        if siblings:
            siblings = [picked[0]] + siblings

    # 6. Whole-doc summaries for the routed docs, so the answer step can see each doc's
    #    gist (title + focus) — not just a chunk sliver. The caller injects these when
    #    `answer_inject_summaries` is on; cross-doc synthesis needs the overview a single
    #    chunk can't give. Cheap: `summaries` is already loaded.
    doc_summaries = []
    for name in picked:
        e = summaries.get(name) or {}
        foc = (e.get("focus") or "").strip() or _doc_summary_text(e)
        if foc:
            doc_summaries.append({"name": name,
                                  "title": (e.get("title") or name),
                                  "focus": foc[:300]})

    return {"results": chunks, "expanded": expanded, "second_pass": False,
            "query_used": query, "routed_docs": picked, "doc_summaries": doc_summaries,
            "siblings": siblings}


def _clean_snippet(text: str) -> str:
    """Strip the indexer's '[Doc: name | Page: N]' prefix and a leading YAML frontmatter
    block so a finder snippet shows readable body text, not metadata. Best-effort."""
    t = (text or "").strip()
    t = re.sub(r"^\[Doc:[^\]]*\]\s*", "", t)
    # Frontmatter may be linearized (newlines collapsed); if the text opens with a '---'
    # block, drop everything up to and including its closing '---'. If there's no closing
    # '---' within this chunk, leave the text as-is (the focus fallback covers that case).
    if t.startswith("---"):
        closing = t.find("---", 3)
        if closing != -1:
            t = t[closing + 3:].strip()
    return re.sub(r"\s+", " ", t)[:240].strip()


def find_pointers(query, col, embed_model, username, group_id,
                  settings=None, max_docs=8):
    """Finder mode: 'show me where to look', NOT 'answer for me'.

    Runs the full card-routed retrieval (`retrieve_carded` — matter-anchor, entity-join,
    per-doc floor all apply) and then AGGREGATES the retrieved chunks by source document into
    a ranked list of pointers. Deliberately runs NO LLM: the lawyer reads the source text, so
    there is no synthesis and no hallucination risk. This plays to the system's strong half
    (retrieval-hit ~0.87 EN / 0.79 NL, near-perfect single-doc) and sidesteps the weak half
    (cross-doc synthesis / harsh judge). Returns, per doc, the best matching section(s) and a
    source snippet so the lawyer can jump straight to the clause.

    Each pointer: {source, title, matter, sections[], score, snippet}. Ranked by best chunk
    score, capped at `max_docs`.
    """
    settings = settings or {}
    res = retrieve_carded(query, col, embed_model, username, group_id, settings=settings)
    chunks = res.get("results") or []
    # doc_summaries gives clean titles for the routed docs; key by name AND basename so a
    # chunk's `source` (which may be a rel-path) still matches the summary entry.
    smap: dict[str, dict] = {}
    for d in (res.get("doc_summaries") or []):
        smap[d["name"]] = d
        smap[d["name"].rsplit("/", 1)[-1]] = d

    by_doc: dict[str, dict] = {}
    for c in chunks:
        src = c.get("source")
        if not src:
            continue
        d = by_doc.setdefault(src, {"source": src, "score": 0.0,
                                    "sections": [], "snippet": ""})
        sc = float(c.get("score") or 0.0)
        if sc > d["score"]:
            d["score"] = sc
            d["snippet"] = _clean_snippet(c.get("text") or "")
        sec = (c.get("heading_path") or "").strip()
        page = c.get("page")
        loc = sec or (f"p.{page}" if page else "")
        if loc and loc not in d["sections"]:
            d["sections"].append(loc)

    out = []
    for src, d in by_doc.items():
        smry = smap.get(src) or smap.get(src.rsplit("/", 1)[-1]) or {}
        d["title"] = smry.get("title") or src.rsplit("/", 1)[-1]
        d["matter"] = _matter_of(src, smry)
        d["sections"] = d["sections"][:3]
        d["score"] = round(d["score"], 4)
        # Snippet = the cleaned matched passage (in the corpus language) so the lawyer sees the
        # actual text they'll read. Fall back to the whole-doc `focus` only if the passage is
        # empty (e.g. letterhead-only page).
        if not d["snippet"]:
            d["snippet"] = (smry.get("focus") or "").strip()[:240]
        out.append(d)
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:max_docs]


def _retrieve_picked(query, col, embed_model, picked, max_chunks, floor, vec):
    """Fetch chunks from the routed docs. With `floor` <= 0 (single-doc path) this is the
    plain global top-`max_chunks` pull restricted to `picked`. With `floor` > 0 (multi-doc
    path) it first takes the top `floor` chunks from EACH picked doc — guaranteeing every
    routed doc is represented — then fills the remaining slots by global score across all
    picked docs. Dedups by (source, page, text-head). Reuses the query vec (no re-embed);
    one Chroma query per doc for the floor pass (picked is small, ≤ multidoc_max_docs)."""
    if floor <= 0 or len(picked) <= 1:
        return retrieve(query, col, max_chunks, embed_model, sources=picked, vec=vec)

    def _key(c):
        return (c.get("source"), c.get("page"), (c.get("text") or "")[:120])

    chosen, seen = [], set()
    # Floor pass: ≥ floor chunks from each picked doc, in picked (ranked) order.
    for name in picked:
        if len(chosen) >= max_chunks:
            break
        for c in retrieve(query, col, floor, embed_model, sources=[name], vec=vec):
            k = _key(c)
            if k not in seen and len(chosen) < max_chunks:
                chosen.append(c); seen.add(k)
    # Fill remainder by global score across all picked docs.
    if len(chosen) < max_chunks:
        for c in retrieve(query, col, max_chunks, embed_model, sources=picked, vec=vec):
            if len(chosen) >= max_chunks:
                break
            k = _key(c)
            if k not in seen:
                chosen.append(c); seen.add(k)
    return chosen
