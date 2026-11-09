"""Local multilingual cross-encoder reranker (bge-reranker-v2-m3).

Runs fully offline on the GPU (CPU fallback) — no external API, no fine-tuning.
Used to pick the best documents from the hybrid card-search candidate pool. It
beats the LLM router on recall (+7pts) at ~10x lower latency and is deterministic
(same query -> same order), which matters for a legal tool.

Lazy singleton: the model loads on first use so app startup and Ollama-only
deployments are unaffected. Fail-open: callers should treat unavailability as
"skip reranking" and fall back to their previous behaviour.

GPU memory pressure: on a shared GPU the Ollama answer model + embedders can
starve the cross-encoder, raising CUDA OOM. Rather than fail open to the (much
worse) LLM router, we drop the cross-encoder to CPU — slower (~20x) but it keeps
the measured recall. Once on CPU we stay there for the process lifetime.
"""
from __future__ import annotations

import threading
from typing import Callable

from local_ai_law.config import log

_MODEL = None
_LOCK = threading.Lock()
_FAILED = False
_DEVICE: str | None = None  # resolved on first load: "cuda" or "cpu"
MODEL_NAME = "BAAI/bge-reranker-v2-m3"
MAX_LENGTH = 512


def available() -> bool:
    """True if the cross-encoder deps are importable (no model load)."""
    if _FAILED:
        return False
    try:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
        return True
    except Exception:
        return False


def _is_oom(exc: Exception) -> bool:
    """True for a CUDA out-of-memory error (torch.OutOfMemoryError or the
    RuntimeError variant some ops raise)."""
    if exc.__class__.__name__ == "OutOfMemoryError":
        return True
    return "out of memory" in str(exc).lower()


def _load(device: str):
    from sentence_transformers import CrossEncoder
    return CrossEncoder(MODEL_NAME, max_length=MAX_LENGTH, device=device)


def _free_cuda():
    """Drop the GPU model and release its VRAM."""
    global _MODEL
    _MODEL = None
    try:
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        log.debug("VRAM release best-effort failed (torch/gc unavailable?)", exc_info=True)


def _switch_to_cpu():
    """Free the GPU model and pin the device to CPU for the rest of the process."""
    global _DEVICE
    with _LOCK:
        _free_cuda()
        _DEVICE = "cpu"


def _model():
    """Lazy-load the cross-encoder on GPU; if the GPU load OOMs, fall back to CPU.
    Thread-safe."""
    global _MODEL, _FAILED, _DEVICE
    if _MODEL is not None:
        return _MODEL
    with _LOCK:
        if _MODEL is not None or _FAILED:
            return _MODEL
        import torch
        want = _DEVICE or ("cuda" if torch.cuda.is_available() else "cpu")
        try:
            _MODEL = _load(want)
            _DEVICE = want
        except Exception as e:
            if want == "cuda" and _is_oom(e):
                _free_cuda()
                try:
                    _MODEL = _load("cpu")
                    _DEVICE = "cpu"
                    log.warning("cross-encoder: GPU OOM on load -> running on CPU")
                except Exception:
                    _FAILED = True
                    raise
            else:
                _FAILED = True
                raise
    return _MODEL


def score(query: str, docs: list, text_fn: Callable[[dict], str]) -> list[float]:
    """Cross-encoder relevance score per doc, aligned to `docs` (not sorted).

    Lets callers fuse the CE ranking with another signal (e.g. RRF) instead of
    only consuming a sorted list. On a GPU OOM it transparently retries on CPU
    (and stays on CPU). Raises only if the model can't run at all — callers
    fail-open.
    """
    if not docs:
        return []
    pairs = [(query, text_fn(d)) for d in docs]
    try:
        raw = _model().predict(pairs)
    except Exception as e:
        if _is_oom(e) and _DEVICE != "cpu":
            _switch_to_cpu()
            log.warning("cross-encoder: GPU OOM on predict -> retrying on CPU")
            raw = _model().predict(pairs)
        else:
            raise
    return [float(s) for s in raw]


def rerank(query: str, docs: list, text_fn: Callable[[dict], str],
           top_k: int | None = None) -> list:
    """Sort `docs` by cross-encoder relevance to `query`, highest first.

    `text_fn(doc)` renders the document text the model scores against the query.
    Returns the (optionally truncated) reordered list. Fail-open on OOM (see
    `score`).
    """
    if not docs:
        return []
    scores = score(query, docs, text_fn)
    order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
    ranked = [docs[i] for i in order]
    return ranked[:top_k] if top_k else ranked
