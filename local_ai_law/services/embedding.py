"""Ollama embedding wrapper  -  100% local."""

from local_ai_law.config import log
from local_ai_law.services.ollama_client import embed

# Qwen3-Embedding is trained for asymmetric, instruction-aware *queries*: the
# query side gets an `Instruct: <task>\nQuery: <q>` prefix while documents are
# embedded raw. Applying it query-side only (no re-index) measurably improves
# retrieval. Generic instruction — no corpus/language assumption. Gated on the
# model family so a different embed model is unaffected.
_QUERY_INSTRUCT = (
    "Instruct: Given a question, retrieve the document that best answers it.\n"
    "Query: "
)


def _uses_qwen3_prefix(model: str) -> bool:
    return "qwen3-embedding" in (model or "").lower()


def embed_texts(texts: list[str], model: str) -> list[list[float]]:
    try:
        return embed(model, texts)
    except Exception as e:
        log.error("Embedding failed: %s", e)
        raise


def embed_query(text: str, model: str) -> list[float]:
    """Embed a single search query with the model's query-side instruction
    prefix when applicable. Use this for questions; use embed_texts for the
    documents/cards/chunks being indexed."""
    q = _QUERY_INSTRUCT + text if _uses_qwen3_prefix(model) else text
    vecs = embed_texts([q], model)
    return vecs[0] if vecs else []
