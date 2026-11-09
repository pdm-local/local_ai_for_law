"""Frontmatter-derived entity index — the cross-matter join for connection questions.

Matter-anchored retrieval (services/retrieval.py:_matter_of) groups sibling docs WITHIN
one dossier. The residual `connection` cases join docs ACROSS matters by a shared entity:
the same party appearing in two matters, or two docs citing the same statute. This module
builds a deterministic inverted index from the document frontmatter the cards already
carry (`parties`, `statutes`, `matter` — populated by doc_summary._apply_frontmatter and
scripts/backfill_matter.py). No LLM, no GPU, no embeddings.

Index shape (data/<user>/entity_index/<group>.json):
    {
      "postings": {"party:<norm>": [doc…], "statute:<norm>": [doc…], "matter:<id>": [doc…]},
      "surfaces": [["meridiaan retail b.v.", "party:<norm>"], …]   # for query substring match
    }
The postings key is normalized (dedup across spelling variants); the surfaces carry the
original lowercased text so a raw query can be matched without NER.
"""
from __future__ import annotations

import re

from local_ai_law.config import atomic_write_json, user_data_dir, _ensure, log
from local_ai_law.services.graph import normalize_concept

# Legal-entity suffixes stripped to form a second, shorter party surface so a question
# naming "Meridiaan Retail" still matches the stored "Meridiaan Retail B.V.".
_ENTITY_SUFFIX = re.compile(
    r"[\s,]+(b\.?v\.?|n\.?v\.?|gmbh|ltd\.?|inc\.?|llc|plc|s\.?a\.?|coöperatie|"
    r"advocaten|holding)\b\.?\s*$", re.IGNORECASE)


def _entity_index_dir(username: str):
    return _ensure(user_data_dir(username) / "entity_index")


def _index_path(group_id: str, username: str):
    return _entity_index_dir(username) / f"{group_id}.json"


def _norm_party(p: str) -> str:
    return normalize_concept(p or "").strip()


def _norm_statute(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _party_surfaces(p: str):
    """Lowercased surface forms for substring matching: the full name and, if it ends in a
    legal-entity suffix, the bare name too. Skip surfaces shorter than 4 chars (too generic
    → false joins)."""
    out = []
    full = (p or "").strip().lower()
    if len(full) >= 4:
        out.append(full)
    bare = _ENTITY_SUFFIX.sub("", full).strip()
    if len(bare) >= 4 and bare != full:
        out.append(bare)
    return out


def build_entity_index(group_id: str, username: str = "default") -> dict:
    """Build (and persist) the entity index for a group from its doc summaries.

    Reads `parties`/`statutes`/`matter` off each summary entry (frontmatter-sourced).
    Fail-open: returns an empty index if there are no summaries. Deterministic; safe to
    re-run (overwrites). Returns the index dict."""
    from local_ai_law.services.doc_summary import load_doc_summaries
    from local_ai_law.services.retrieval import _matter_of

    summaries = load_doc_summaries(group_id, username)
    postings: dict[str, list[str]] = {}
    surfaces: dict[str, str] = {}          # surface text → key (dedup surfaces)

    def _add(key: str, doc: str):
        lst = postings.setdefault(key, [])
        if doc not in lst:
            lst.append(doc)

    for name, e in summaries.items():
        if not isinstance(e, dict):
            continue
        for p in (e.get("parties") or []):
            norm = _norm_party(p)
            if not norm:
                continue
            key = f"party:{norm}"
            _add(key, name)
            for surf in _party_surfaces(p):
                surfaces.setdefault(surf, key)
        for st in (e.get("statutes") or []):
            norm = _norm_statute(st)
            if not norm:
                continue
            key = f"statute:{norm}"
            _add(key, name)
            s = (st or "").strip().lower()
            if len(s) >= 4:
                surfaces.setdefault(s, key)
        m = _matter_of(name, e)
        if m:
            _add(f"matter:{m}", name)

    n_docs = sum(1 for e in summaries.values() if isinstance(e, dict))
    index = {"n_docs": n_docs,
             "postings": postings,
             "surfaces": sorted([s, k] for s, k in surfaces.items())}
    atomic_write_json(_index_path(group_id, username), index)
    return index


def load_entity_index(group_id: str, username: str = "default") -> dict:
    """Load the group's entity index, or an empty index if none built yet."""
    p = _index_path(group_id, username)
    if p.exists():
        try:
            import json
            d = json.loads(p.read_text())
            if isinstance(d, dict) and "postings" in d:
                return d
        except Exception as e:
            # Best-effort: a corrupt/partial index must not break retrieval — fall through
            # to the empty index (entity-join just contributes nothing). Log it though.
            log.warning("entity index load failed for %s/%s: %s", username, group_id, e)
    return {"n_docs": 0, "postings": {}, "surfaces": []}


# A generic hub (the Dutch DPA, a statute everyone cites) is useless as a *connection* key.
# Two independent hub signals: spanning > _HUB_FRAC of all docs, OR — the one that matters
# for a cross-matter join — appearing in more than _HUB_MATTERS distinct matters. The DPA
# sits in ~2 docs across 19 matters: few docs, but a matter hub. Mirrors _keyword_expand's
# df_cap idea, lifted to the matter level.
_HUB_FRAC = 0.33
_HUB_MATTERS = 6
_MATTER_TOK = re.compile(r"\b\d{4}[.\-](\d{3,4})\b|-(\d{3,4})-")


def _matters_spanned(docs: list[str]) -> int:
    out = set()
    for d in docs:
        m = _MATTER_TOK.search(d or "")
        if m:
            out.add(m.group(1) or m.group(2))
    return len(out)


def keys_in_query(query: str, index: dict, *, drop_hubs: bool = True) -> list[str]:
    """Posting keys whose surface text appears in the query (zero-NER substring match).
    Returns DISTINCT keys, longest-surface-first so a specific party wins over a generic
    fragment. Excludes `matter:` keys (matter is handled by the anchored router) and, when
    `drop_hubs`, generic hubs (span > _HUB_FRAC of docs or > _HUB_MATTERS matters)."""
    q = (query or "").lower()
    postings = index.get("postings") or {}
    n_docs = int(index.get("n_docs") or 0)
    hub_cap = max(3, int(_HUB_FRAC * n_docs)) if n_docs else None
    hits: list[str] = []
    seen: set[str] = set()
    for surf, key in sorted(index.get("surfaces") or [], key=lambda x: -len(x[0])):
        if key in seen or key.startswith("matter:"):
            continue
        if surf and surf in q:
            docs = postings.get(key, [])
            is_hub = (hub_cap and len(docs) > hub_cap) or _matters_spanned(docs) > _HUB_MATTERS
            if drop_hubs and is_hub:
                seen.add(key)   # acknowledge but don't use a hub
                continue
            hits.append(key)
            seen.add(key)
    return hits


def join_docs(query: str, index: dict, max_docs: int, exclude: set[str] | None = None) -> list[str]:
    """Cross-matter connection join: docs sharing the entities named in the query.

    Ranks by how many of the query's entity keys a doc carries (the intersection — the
    actual "connection" — first), then by total postings. Returns ≤ `max_docs` doc names
    not already in `exclude`. Empty when the query names no known entity."""
    keys = keys_in_query(query, index)
    if len(keys) < 1:
        return []
    postings = index.get("postings") or {}
    score: dict[str, int] = {}
    for key in keys:
        for doc in postings.get(key, []):
            score[doc] = score.get(doc, 0) + 1
    exclude = exclude or set()
    # Prefer docs in the intersection of ≥2 query entities when the query names ≥2;
    # otherwise (single entity) fall back to that entity's docs.
    ranked = sorted((d for d in score if d not in exclude),
                    key=lambda d: (-score[d], d))
    if len(keys) >= 2:
        inter = [d for d in ranked if score[d] >= 2]
        if inter:
            ranked = inter
    return ranked[:max_docs]
