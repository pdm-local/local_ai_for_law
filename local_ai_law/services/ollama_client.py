"""Shared Ollama access with local GPU concurrency controls."""

from __future__ import annotations

import subprocess
import sys
import threading
import json
import urllib.request
from contextlib import contextmanager
from typing import Iterable

import ollama

from local_ai_law.config import (
    EMBED_BATCH_SIZE,
    MAX_OLLAMA_NUM_CTX,
    OLLAMA_CHAT_CONCURRENCY,
    OLLAMA_HOST,
    OLLAMA_TIMEOUT,
    log,
    ollama_keep_alive,
    ollama_options,
)

_gate_cond = threading.Condition()
_gate_limit = OLLAMA_CHAT_CONCURRENCY
_gate_active = 0
_active_model = ""   # model currently loaded on the GPU; guarded by _gate_cond

_client = ollama.Client(host=OLLAMA_HOST, timeout=OLLAMA_TIMEOUT)


def _ollama_json(path: str, payload: dict | None = None, timeout: int = 5):
    host = OLLAMA_HOST.rstrip("/")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"{host}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def set_generation_concurrency(limit: int):
    """Set how many generations may hold the GPU slot at once (min 1); wakes waiters."""
    global _gate_limit
    with _gate_cond:
        _gate_limit = max(1, int(limit))
        _gate_cond.notify_all()


def generation_concurrency() -> int:
    """Current max concurrent-generation limit for the GPU slot."""
    with _gate_cond:
        return _gate_limit


@contextmanager
def ollama_generation_slot(model: str | None = None):
    """Limit concurrent generation (one local GPU) AND make model switching safe.

    The previous design released the switch lock before the actual chat/embed call,
    so with concurrency > 1 a second thread could unload/switch the model while the
    first was mid-generation (VRAM spike / corrupt output). Here the switch happens
    inside the gate and ONLY when no other generation is in flight (`_gate_active==0`),
    and a generation increments `_gate_active` while holding the lock — so once a call
    starts, `_active_model` cannot change under it. Calls that share the already-loaded
    model still run concurrently up to the gate limit.
    """
    global _gate_active, _active_model
    with _gate_cond:
        while True:
            if _gate_active >= _gate_limit:
                _gate_cond.wait(); continue
            # A model switch must wait for in-flight generations to drain (one GPU).
            if model and _active_model and _active_model != model and _gate_active > 0:
                _gate_cond.wait(); continue
            break
        if model and _active_model != model:
            if _active_model:
                try:
                    _client.generate(model=_active_model, keep_alive=0)
                except Exception as e:
                    log.warning("Failed to unload model %s: %s", _active_model, e)
            _active_model = model
        _gate_active += 1
    try:
        yield
    finally:
        with _gate_cond:
            _gate_active = max(0, _gate_active - 1)
            _gate_cond.notify_all()


def chat(model: str, messages: list[dict], *, stream: bool = False, num_ctx: int | None = None,
         options: dict | None = None, keep_alive: str | None = None):
    """Run an Ollama chat completion, holding the GPU slot for the call. Returns the
    response, or a generator of chunks when `stream=True`. Thinking is disabled."""
    opts = ollama_options(num_ctx, **(options or {}))
    if stream:
        def _stream():
            with ollama_generation_slot(model):
                yield from _client.chat(
                    model=model,
                    messages=messages,
                    stream=True,
                    think=False,
                    options=opts,
                    keep_alive=keep_alive or ollama_keep_alive(),
                )
        return _stream()
    with ollama_generation_slot(model):
        return _client.chat(
            model=model,
            messages=messages,
            stream=False,
            think=False,
            options=opts,
            keep_alive=keep_alive or ollama_keep_alive(),
        )


def embed(model: str, texts: list[str], *, keep_alive: str | None = None):
    """Embed a batch of texts via Ollama, holding the GPU slot. Returns the vectors."""
    with ollama_generation_slot(model):
        return _client.embed(model=model, input=texts, keep_alive=keep_alive or ollama_keep_alive()).embeddings


def iter_batches(items: list[str], size: int | None = None) -> Iterable[list[str]]:
    """Yield items in batches (default EMBED_BATCH_SIZE, capped at 128)."""
    batch_size = max(1, min(int(size or EMBED_BATCH_SIZE), 128))
    for i in range(0, len(items), batch_size):
        yield items[i:i + batch_size]


def list_models() -> list[str]:
    """List installed Ollama model names (via /api/tags)."""
    models = list_model_records()
    names: list[str] = []
    for m in models:
        if isinstance(m, dict):
            name = m.get("model") or m.get("name") or ""
        else:
            name = getattr(m, "model", None) or getattr(m, "name", "")
        if name:
            names.append(name)
    return names


def list_model_records() -> list:
    """Raw model records from Ollama /api/tags (dicts with model/size/details)."""
    resp = _ollama_json("/api/tags")
    return resp.get("models", []) if isinstance(resp, dict) else []


def show_model(name: str):
    """Ollama /api/show details for one model (capabilities, template, model_info)."""
    return _ollama_json("/api/show", {"model": name})


def diagnostics(configured_models: dict[str, str] | None = None) -> dict:
    """Health snapshot: Ollama reachability, installed vs configured models, GPU info,
    and any warnings — used by the system diagnostics endpoint."""
    warnings: list[str] = []
    ollama_ok = False
    models: list[str] = []
    try:
        models = list_models()
        ollama_ok = True
    except Exception as e:
        warnings.append(f"Ollama is not reachable at {OLLAMA_HOST}: {e}")

    gpu = {"visible": False, "name": "", "memory_total_mb": 0, "memory_used_mb": 0, "driver": ""}
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = proc.stdout.strip().splitlines()
        if not lines:
            raise ValueError("nvidia-smi returned no output")
        line = lines[0]
        name, total, used, driver = [p.strip() for p in line.split(",", 3)]
        gpu = {
            "visible": True,
            "name": name,
            "memory_total_mb": int(float(total)),
            "memory_used_mb": int(float(used)),
            "driver": driver,
        }
    except Exception as e:
        warnings.append(f"NVIDIA GPU diagnostics unavailable: {e}")

    configured = configured_models or {}
    missing = {
        role: model
        for role, model in configured.items()
        if model and models and model not in models
    }
    for role, model in missing.items():
        warnings.append(f"Configured {role} model is not installed in Ollama: {model}")

    if gpu.get("memory_total_mb") and gpu["memory_total_mb"] < 10000:
        warnings.append("GPU VRAM is below the recommended 10 GB local-inference profile.")

    return {
        "python": sys.version.split()[0],
        "ollama": {
            "ok": ollama_ok,
            "host": OLLAMA_HOST,
            "models": models,
            "timeout_sec": OLLAMA_TIMEOUT,
            "generation_concurrency": generation_concurrency(),
            "max_num_ctx": MAX_OLLAMA_NUM_CTX,
            "embed_batch_size": EMBED_BATCH_SIZE,
        },
        "gpu": gpu,
        "models": {"configured": configured, "missing": missing},
        "warnings": warnings,
    }
