"""Query expansion for retrieval — adds related terminology before search.

Legal/procurement questions often use different vocabulary than the source
documents (e.g. "exit" vs "termination", "supplier" vs "contractor"), and the
answer may be spread across headings, tables, and explanatory paragraphs.
One cheap, bounded LLM call broadens the search query with synonyms and
category names so retrieval covers those gaps.

Fail-open by design: any error returns the original query unchanged.
"""

from __future__ import annotations

import re

from local_ai_law.config import log
from local_ai_law.services.ollama_client import chat as ollama_chat

_EXPANSION_PROMPT = (
    "List up to 8 alternative search terms for the query below: synonyms, "
    "domain terminology, official category or clause names, and closely related "
    "concepts. Use the same language as the query. "
    "Reply with a comma-separated list only — no numbering, no explanations.\n"
    "Query: {query}\n"
    "Terms:"
)

_MAX_EXPANDED_CHARS = 700


def should_expand(query: str, mode: str) -> bool:
    """mode: "always" | "auto" (only short queries, which need it most) | "off"."""
    if mode == "always":
        return True
    if mode == "auto":
        return len(query.split()) <= 12
    return False


def expand_query(query: str, settings: dict) -> str:
    """Append LLM-suggested related terms to the query. Returns the original
    query on any failure — expansion must never break retrieval."""
    model = settings.get("chat_model", "")
    if not model or not query.strip():
        return query
    try:
        resp = ollama_chat(
            model,
            [{"role": "user", "content": _EXPANSION_PROMPT.format(query=query[:500])}],
            stream=False,
            num_ctx=1024,
            # num_predict bounds generation time far more reliably than a
            # wall-clock timeout; temp 0 keeps the term list deterministic
            options={"temperature": 0, "num_predict": 80},
        )
        raw = (resp.get("message", {}) or {}).get("content", "") or ""
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL)
        terms = [t.strip(" .;\n\t-•") for t in raw.split(",")]
        q_lower = query.lower()
        fresh = []
        for t in terms:
            # whole-word match — substring would drop e.g. "act" because the
            # query contains "contract"
            if not t or len(t) > 60 or re.search(rf"\b{re.escape(t.lower())}\b", q_lower):
                continue
            if t.lower() in (f.lower() for f in fresh):
                continue
            fresh.append(t)
        if not fresh:
            return query
        expanded = f"{query} {' '.join(fresh[:8])}"
        return expanded[:_MAX_EXPANDED_CHARS]
    except Exception as e:
        log.debug("query expansion failed (using raw query): %s", e)
        return query
