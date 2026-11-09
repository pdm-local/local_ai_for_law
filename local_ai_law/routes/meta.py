"""Health check, models list, meta, branding, and edition endpoints."""

from fastapi import APIRouter, Depends

from local_ai_law.config import (
    DEFAULT_GROUP_COLORS, DEFAULT_GROUP_EMOJIS, get_branding, get_disclaimer,
    DIFFICULTY_PROMPTS, SUPPORTED_LANGUAGES, DEFAULT_LANGUAGE,
    load_edition_config, get_active_edition, load_settings, log,
)
from local_ai_law.routes.auth import require_admin, require_auth
from local_ai_law.services.ollama_client import (
    diagnostics as ollama_diagnostics,
    list_model_records,
    list_models,
    show_model,
)

router = APIRouter(prefix="/api", tags=["meta"])

_THINKING_FAMILIES = {"qwen3", "qwen3.5", "deepseek-r1", "qwq"}
_VISION_KEYS = {
    "vision", "clip", "vl", "v-l", "llava", "bakllava", "moondream",
    "gemma3", "minicpm-v", "minicpmv", "qwen-vl", "qwen2-vl", "qwen2.5-vl",
}
_TOOL_FAMILIES = {"llama3", "mistral", "qwen2", "qwen3", "command-r"}
_EMBED_KEYS = {"embed", "embedding", "nomic", "bge", "mxbai", "minilm", "snowflake", "e5"}
_CODER_KEYS = {"coder", "code", "codestral", "starcoder", "codegemma", "deepseek-coder"}


def _model_name(m) -> str:
    if isinstance(m, dict):
        return m.get("model") or m.get("name") or ""
    return getattr(m, "model", None) or getattr(m, "name", "")


def _model_get(m, key: str, default=None):
    if isinstance(m, dict):
        return m.get(key, default)
    return getattr(m, key, default)


def _models_from_response(resp) -> list:
    if isinstance(resp, dict):
        return resp.get("models", [])
    return getattr(resp, "models", []) or []


def _detect_capabilities(name: str, info: dict) -> dict:
    n = name.lower()
    modelfile = str(info.get("modelfile", "")).lower()
    template = str(info.get("template", "")).lower()
    details = info.get("details", {}) or {}
    families = details.get("families", []) if isinstance(details, dict) else []
    combined = f"{n} {modelfile} {template} {' '.join(str(f).lower() for f in families)}"
    role_blob = f"{n} {str(details.get('family', '')).lower()} {' '.join(str(f).lower() for f in families)}"

    ctx = 4096
    params = info.get("model_info", {}) or {}
    if isinstance(params, dict):
        for key, value in params.items():
            if "context" in str(key).lower():
                try:
                    ctx = int(value)
                    break
                except Exception:
                    log.debug("Non-integer context_length %r in model_info; ignoring", value, exc_info=True)

    # Prefer Ollama's authoritative capability list (/api/show → "capabilities");
    # fall back to the name heuristic only when it is absent. Without this, every
    # "gemma3*" name is treated as vision even when the variant is text-only.
    caps = info.get("capabilities")
    caps = [str(c).lower() for c in caps] if isinstance(caps, (list, tuple)) else []
    has_vision = ("vision" in caps) if caps else any(k in role_blob for k in _VISION_KEYS)
    is_embedding = ("embedding" in caps) if caps else any(k in role_blob for k in _EMBED_KEYS)
    is_coder = any(k in role_blob for k in _CODER_KEYS)
    is_chat = not is_embedding

    tags = []
    if is_chat:
        tags.extend(["chat", "text"])
    if is_embedding:
        tags.append("embedding")
    if has_vision:
        tags.append("vision")
    if is_coder:
        tags.append("coding")

    return {
        "has_thinking": any(t in combined for t in _THINKING_FAMILIES) or "think" in template,
        "has_vision": has_vision,
        "has_tools": any(t in combined for t in _TOOL_FAMILIES),
        "is_embedding": is_embedding,
        "is_coder": is_coder,
        "is_chat": is_chat,
        "context_length": ctx,
        "tags": tags,
    }


@router.get("/health")
async def health():
    """GET (public) — liveness probe: app status + whether Ollama is reachable."""
    ollama_ok = False
    warnings_count = 0
    try:
        list_models()
        ollama_ok = True
    except Exception:
        warnings_count = 1
    return {
        "status": "ok",
        "ollama": ollama_ok,
        "gpu_visible": None,
        "warnings_count": warnings_count,
    }


@router.get("/system/diagnostics")
async def api_system_diagnostics(admin: dict = Depends(require_admin)):
    """GET (admin only): deep diagnostics: configured models, Ollama/GPU status, job pool, settings."""
    cfg = load_settings(admin["sub"])
    configured = {
        "chat": cfg.get("chat_model", ""),
        "embed": cfg.get("embed_model", ""),
        "wiki": cfg.get("wiki_model", ""),
        "relation": cfg.get("relation_model", ""),
        "vision": cfg.get("vision_model", ""),
    }
    diag = ollama_diagnostics(configured)
    try:
        from local_ai_law.services.jobs import get_pool_status
        jobs = get_pool_status()
    except Exception as e:
        jobs = {"error": str(e)}
        diag["warnings"].append(f"Job diagnostics unavailable: {e}")
    diag["app"] = {
        "name": get_branding()["app_name"],
        "edition": get_active_edition(),
    }
    diag["settings"] = {
        "num_ctx": cfg.get("num_ctx"),
        "auto_wiki_build": cfg.get("auto_wiki_build", False),
        "auto_graph_build": cfg.get("auto_graph_build", False),
        "auto_contradictions": cfg.get("auto_contradictions", False),
        "ollama_chat_concurrency": cfg.get("ollama_chat_concurrency"),
        "embed_batch_size": cfg.get("embed_batch_size"),
    }
    diag["jobs"] = jobs
    return diag


@router.get("/models")
async def api_models(admin: dict = Depends(require_admin)):
    """GET (admin only): the list of available Ollama model names (empty list + error on failure)."""
    try:
        return {"models": list_models()}
    except Exception as e:
        log.warning("Could not list models: %s", e)
        return {"models": [], "error": str(e)}


@router.get("/models/rich")
async def api_models_rich(admin: dict = Depends(require_admin)):
    """GET (admin only): models with detected capabilities (chat/vision/embedding/coder, context length)."""
    try:
        result = []
        for m in list_model_records():
            name = _model_name(m)
            if not name:
                continue
            try:
                info = show_model(name)
            except Exception:
                info = {}
            if not isinstance(info, dict) and hasattr(info, "model_dump"):
                info = info.model_dump()
            result.append({
                "name": name,
                "modified_at": str(_model_get(m, "modified_at", "")),
                "size": int(_model_get(m, "size", 0) or 0),
                "details": _model_get(m, "details", {}) or {},
                "capabilities": _detect_capabilities(name, info if isinstance(info, dict) else {}),
            })
        return result
    except Exception as e:
        log.warning("Could not list rich models: %s", e)
        return []


@router.get("/meta")
async def api_meta():
    """GET (public) — UI constants for the login screen: colors, emojis, branding,
    disclaimer, difficulty levels, and supported languages."""
    return {
        "colors":            DEFAULT_GROUP_COLORS,
        "emojis":            DEFAULT_GROUP_EMOJIS,
        "branding":          get_branding(),
        "disclaimer":        get_disclaimer(),
        "difficulty_levels": list(DIFFICULTY_PROMPTS.keys()),
        "languages":         SUPPORTED_LANGUAGES,
        "default_language":  DEFAULT_LANGUAGE,
    }


@router.get("/edition/modules")
async def edition_modules(user: dict = Depends(require_auth)):
    """
    Return the set of enabled modules, wiki page types, and edition metadata
    for the current instance. Used by the frontend to show/hide nav items.
    """
    cfg     = load_edition_config()
    edition = get_active_edition()

    from local_ai_law.services.editions import EDITIONS
    ed = EDITIONS.get(edition)

    return {
        "edition":             edition,
        "edition_name":        ed.name if ed else edition,
        "preset":              cfg.get("preset"),
        "enabled_modules":     cfg.get("enabled_modules", []),
        "wiki_page_types":     cfg.get("wiki_page_types", []),
        "wiki_first_retrieval": cfg.get("wiki_first_retrieval", True),
        "wiki_enabled":        cfg.get("wiki_enabled", True),
        "graph_enabled":       cfg.get("graph_enabled", True),
        "save_back_enabled":   cfg.get("save_back_enabled", False),
        "sensitivity_enforcement": cfg.get("sensitivity_enforcement", False),
    }
