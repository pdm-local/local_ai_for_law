"""Settings endpoints  -  per-user settings and user profile."""

import base64
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from local_ai_law.config import (
    USER_SETTING_KEYS, load_settings, save_settings_file, user_data_dir, atomic_write_json, log,
)
from local_ai_law.models import SettingsIn
from local_ai_law.routes.auth import get_current_username, require_auth
from local_ai_law.services.audit import audit_log

router = APIRouter(prefix="/api", tags=["settings"])


def _visible(settings: dict, user: dict) -> dict:
    """An admin sees every setting; anyone else only USER_SETTING_KEYS (no model,
    backend or retrieval detail)."""
    if user.get("role") == "admin":
        return settings
    return {k: settings[k] for k in USER_SETTING_KEYS if k in settings}


@router.get("/settings")
async def api_get_settings(user: dict = Depends(require_auth)):
    """GET: the user's effective settings (global defaults merged with overrides, the
    .env technical keys last), cut to USER_SETTING_KEYS for a user who is not an admin."""
    return _visible(load_settings(user["sub"]), user)


@router.post("/settings")
async def api_post_settings(body: SettingsIn, user: dict = Depends(require_auth)):
    """POST: persist changed settings (only non-null fields). A user who is not an admin
    can change only USER_SETTING_KEYS; anything else they send is ignored. Applies the
    Ollama concurrency limit immediately if an admin changed it."""
    username = user["sub"]
    admin = user.get("role") == "admin"
    s = load_settings(username)
    for k, v in body.model_dump(exclude_none=True).items():
        if admin or k in USER_SETTING_KEYS:
            s[k] = v
    save_settings_file(s, username)
    if admin and body.ollama_chat_concurrency is not None:
        from local_ai_law.services.ollama_client import set_generation_concurrency
        set_generation_concurrency(body.ollama_chat_concurrency)
    audit_log(username, "update_settings")
    return _visible(load_settings(username), user)


# ── Profile ───────────────────────────────────────────────────────────────────

_MAX_AVATAR_BYTES = 2 * 1024 * 1024  # 2 MB
_ALLOWED_AVATAR_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def _profile_path(username: str) -> Path:
    return user_data_dir(username) / "profile.json"


def _load_profile(username: str) -> dict:
    p = _profile_path(username)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            log.debug("Unreadable profile.json for %s; using defaults", username, exc_info=True)
    return {"display_name": "", "bio": "", "avatar_data_url": ""}


@router.get("/profile")
async def api_get_profile(username: str = Depends(get_current_username)):
    """GET — the user's display name, bio, and avatar data URL."""
    return _load_profile(username)


@router.post("/profile")
async def api_save_profile(body: dict, username: str = Depends(get_current_username)):
    """POST — update profile fields; validates the avatar data URL's MIME and 2 MB size cap."""
    profile = _load_profile(username)
    display_name = str(body.get("display_name", "")).strip()[:100]
    bio = str(body.get("bio", "")).strip()[:500]
    if "avatar_data_url" in body:
        raw = str(body["avatar_data_url"])
        if raw and not raw.startswith("data:image/"):
            raise HTTPException(400, "avatar_data_url must be a data: URL")
        # Validate MIME and size
        if raw:
            parts = raw.split(",", 1)
            if len(parts) != 2:
                raise HTTPException(400, "Invalid data URL format")
            header = parts[0]  # e.g. "data:image/jpeg;base64"
            mime = header.split(":")[1].split(";")[0] if ":" in header else ""
            if mime not in _ALLOWED_AVATAR_MIMES:
                raise HTTPException(400, f"Unsupported image type: {mime}")
            try:
                decoded = base64.b64decode(parts[1])
            except Exception as e:
                raise HTTPException(400, "Invalid base64 data") from e
            if len(decoded) > _MAX_AVATAR_BYTES:
                raise HTTPException(400, "Avatar image too large (max 2 MB)")
        profile["avatar_data_url"] = raw
    profile["display_name"] = display_name
    profile["bio"] = bio
    atomic_write_json(_profile_path(username), profile)
    audit_log(username, "update_profile")
    return profile
