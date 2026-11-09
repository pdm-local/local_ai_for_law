"""
Cross-document contradiction detection  -  identifies conflicting claims
across documents within a group using LLM analysis.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from local_ai_law.config import (
    user_data_dir, load_settings, DEFAULT_SETTINGS, log,
    ollama_keep_alive, atomic_write_json,
)
from local_ai_law.services.ollama_client import chat as ollama_chat


def _contradictions_path(username: str, group_id: str) -> Path:
    d = user_data_dir(username) / "contradictions"
    d.mkdir(exist_ok=True)
    return d / f"{group_id}.json"


def load_contradictions(username: str, group_id: str) -> dict:
    p = _contradictions_path(username, group_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def detect_contradictions(username: str, group_id: str, col) -> dict:
    """
    Analyse overlapping topics across documents in a group to find contradictions.
    This is computationally expensive  -  intended to run as a background task.
    """
    cfg = load_settings(username)
    model = cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])

    total = col.count()
    if total < 4:
        result = {
            "contradictions": [],
            "status": "insufficient_data",
            "message": "Need at least 4 indexed chunks to detect contradictions.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(_contradictions_path(username, group_id), result)
        return result

    # Get all documents and group chunks by source
    all_data = col.get()
    by_source: dict[str, list[str]] = {}
    for doc, meta in zip(all_data["documents"], all_data["metadatas"]):
        src = meta.get("source", "?")
        by_source.setdefault(src, []).append(doc[:300])

    sources = list(by_source.keys())
    if len(sources) < 2:
        result = {
            "contradictions": [],
            "status": "single_source",
            "message": "Need documents from multiple sources to detect contradictions.",
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        atomic_write_json(_contradictions_path(username, group_id), result)
        return result

    # Compare pairs of documents (limit to avoid explosion)
    contradictions = []
    pairs_checked = 0
    max_pairs = min(len(sources) * (len(sources) - 1) // 2, 10)

    for i in range(len(sources)):
        for j in range(i + 1, len(sources)):
            if pairs_checked >= max_pairs:
                break

            src_a, src_b = sources[i], sources[j]
            sample_a = "\n".join(by_source[src_a][:3])
            sample_b = "\n".join(by_source[src_b][:3])

            prompt = (
                f"Compare these excerpts from two legal documents and identify any factual "
                f"contradictions or conflicting claims between them.\n\n"
                f'DOCUMENT A ("{src_a}"):\n{sample_a}\n\n'
                f'DOCUMENT B ("{src_b}"):\n{sample_b}\n\n'
                f"If there are contradictions, respond with a JSON array where each element has:\n"
                f'  "claim_a": what document A says,\n'
                f'  "claim_b": what document B says (contradicting),\n'
                f'  "topic": the topic of the contradiction,\n'
                f'  "severity": "high", "medium", or "low"\n\n'
                f"If there are NO contradictions, respond with an empty JSON array: []\n"
                f"Respond with ONLY the JSON array."
            )

            try:
                resp = ollama_chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    num_ctx=4096,
                    options={"temperature": 0.1},
                    keep_alive=ollama_keep_alive(),
                )
                raw = resp["message"]["content"].strip()
                start = raw.find("[")
                end = raw.rfind("]") + 1
                if start >= 0 and end > start:
                    found = json.loads(raw[start:end])
                    for c in found:
                        c["source_a"] = src_a
                        c["source_b"] = src_b
                    contradictions.extend(found)
            except Exception as e:
                log.warning("Contradiction check failed for %s vs %s: %s", src_a, src_b, e)

            pairs_checked += 1

        if pairs_checked >= max_pairs:
            break

    result = {
        "contradictions": contradictions,
        "pairs_checked": pairs_checked,
        "sources_analysed": len(sources),
        "status": "complete",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(_contradictions_path(username, group_id), result)
    log.info("Contradiction detection complete for %s/%s: %d found",
             username, group_id, len(contradictions))
    return result
