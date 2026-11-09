"""Candidate reranker for retrieval — one bounded LLM-as-judge pass that reorders
the candidate documents by relevance to the question, before the router picks.

The hybrid BM25 + vector pool surfaces the *similar* documents but its ordering is
only as good as the (small) embedding model. A single cheap LLM call that reranks
the pool puts the strongest candidates first, so the downstream document router
chooses from a better-ordered list.

Cost is bounded to ONE call (not per-candidate) to stay cheap on a single local
GPU. Fail-open by design: any error or unparseable reply returns the input order
unchanged — reranking must never break retrieval.
"""

from __future__ import annotations

import json
import re

from local_ai_law.config import log
from local_ai_law.services.ollama_client import chat as ollama_chat

_RERANK_PROMPT = (
    "You are reranking candidate documents by how well each one answers the question.\n"
    "Score every candidate from 0 (irrelevant) to 10 (directly answers it), judging by "
    "its summary and excerpt.\n\n"
    "Question: {query}\n\n"
    "Candidates (numbered):\n{candidates}\n\n"
    'Reply with ONLY a JSON array of objects {{"n": <number>, "score": <0-10>}} for every '
    'candidate, e.g. [{{"n": 1, "score": 8}}, {{"n": 2, "score": 3}}]. No prose.'
)


def _format_candidates(candidates: list[dict]) -> str:
    lines = []
    for i, c in enumerate(candidates, 1):
        name = c.get("name", "")
        title = (c.get("title") or "").strip().replace("\n", " ")
        parties = c.get("parties") or []
        summary = (c.get("summary") or "").strip().replace("\n", " ")
        snippet = (c.get("snippet") or "").strip().replace("\n", " ")
        line = f"{i}. {name}"
        if title:
            line += f"  | title: {title[:160]}"
        if parties:
            line += f"  | parties: {', '.join(str(p) for p in parties[:6])}"
        if summary:
            line += f"\n   {summary[:160]}"
        if snippet:
            line += f"\n   card: {snippet[:200]}"
        lines.append(line)
    return "\n".join(lines)


def rerank(query: str, candidates: list[dict], settings: dict) -> list[dict]:
    """Return `candidates` reordered by LLM-judged relevance (descending).

    `candidates`: list of {name, snippet, summary, ...} (extra keys preserved).
    Returns the input list unchanged on any failure so retrieval never breaks.
    """
    model = settings.get("reranker_model") or settings.get("chat_model", "")
    if not model or not query.strip() or len(candidates) < 2:
        return candidates
    try:
        prompt = _RERANK_PROMPT.format(
            query=query[:800],
            candidates=_format_candidates(candidates),
        )
        resp = ollama_chat(
            model,
            [{"role": "user", "content": prompt}],
            stream=False,
            num_ctx=4096,
            # temp 0 keeps scoring deterministic; num_predict bounds latency.
            options={"temperature": 0, "num_predict": 200},
        )
        raw = (resp.get("message", {}) or {}).get("content", "") or ""
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL)
        start, end = raw.find("["), raw.rfind("]") + 1
        if start < 0 or end <= start:
            return candidates
        scored = json.loads(raw[start:end])
        # Map 1-based candidate number -> score; default any missing to 0.
        score_by_idx: dict[int, float] = {}
        for item in scored:
            if isinstance(item, dict):
                n = item.get("n")
                if isinstance(n, (int, float)):
                    score_by_idx[int(n)] = float(item.get("score", 0) or 0)
        if not score_by_idx:
            return candidates
        # Stable sort: preserves original order among equal scores (incl. unscored).
        order = list(range(len(candidates)))
        order.sort(key=lambda i: score_by_idx.get(i + 1, 0.0), reverse=True)
        return [candidates[i] for i in order]
    except Exception as e:
        log.debug("rerank failed (keeping original order): %s", e)
        return candidates
