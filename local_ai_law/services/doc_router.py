"""Document router for retrieval — picks the most relevant document(s) for a
question before fetching chunks.

With hundreds of documents per matter, retrieving chunks across the whole group
dilutes the answer context and can fill the model's context window. Instead we:
  1. narrow to a handful of candidate documents by vector similarity (caller),
  2. let one cheap, bounded LLM call decide which of those documents actually
     answer the question (this module),
  3. fetch a small number of chunks ONLY from the picked documents (caller).

Fail-open by design: any error or an empty/unparseable pick returns [], and the
caller falls back to its top vector candidates.
"""

from __future__ import annotations

import json
import re

from local_ai_law.config import log
from local_ai_law.services.ollama_client import chat as ollama_chat

_ROUTER_PROMPT = (
    "You are routing a question to the document(s) that can answer it.\n"
    "Pick at most {max_docs} of the candidate documents below — only the ones truly "
    "relevant to the question. Prefer fewer. Use the document names exactly as given.\n\n"
    "Question: {query}\n\n"
    "Candidate documents:\n{candidates}\n\n"
    'Reply with ONLY a JSON array of the chosen document names, e.g. ["name-a.md", "name-b.md"]. '
    "No prose, no explanation."
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
            line += f"\n   {summary[:200]}"
        if snippet:
            line += f"\n   card: {snippet[:250]}"
        lines.append(line)
    return "\n".join(lines)


def route_documents(query: str, candidates: list[dict], settings: dict, *,
                    max_docs: int = 3) -> list[str]:
    """Return the subset of candidate document names the LLM judges relevant.

    `candidates`: list of {name, snippet, summary}. Returns [] on any failure so
    the caller can fall back — routing must never break retrieval.
    """
    model = settings.get("chat_model", "")
    if not model or not query.strip() or not candidates:
        return []
    valid_names = {c.get("name", "") for c in candidates}
    try:
        prompt = _ROUTER_PROMPT.format(
            max_docs=max_docs,
            query=query[:800],
            candidates=_format_candidates(candidates),
        )
        resp = ollama_chat(
            model,
            [{"role": "user", "content": prompt}],
            stream=False,
            num_ctx=4096,
            # num_predict bounds generation time; temp 0 keeps the pick deterministic
            options={"temperature": 0, "num_predict": 120},
        )
        raw = (resp.get("message", {}) or {}).get("content", "") or ""
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL)
        start, end = raw.find("["), raw.rfind("]") + 1
        if start < 0 or end <= start:
            return []
        picked = json.loads(raw[start:end])
        # Keep only valid names, preserve LLM order, dedupe, cap at max_docs.
        out: list[str] = []
        for name in picked:
            if isinstance(name, str) and name in valid_names and name not in out:
                out.append(name)
            if len(out) >= max_docs:
                break
        return out
    except Exception as e:
        log.debug("document routing failed (falling back): %s", e)
        return []
