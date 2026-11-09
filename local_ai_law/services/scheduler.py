"""
User registry helper.

This legal workspace has no background education scheduler. Document indexing runs
on upload; backups run via their own scheduler in app.py. The only surviving helper
records usernames on first login.
"""

from __future__ import annotations

import json

from local_ai_law.config import log, DATA_DIR, atomic_write_json

_USER_REGISTRY = DATA_DIR / "user_registry.json"


def register_user_in_registry(username: str):
    """Add username to registry on first login. Call from auth on successful login."""
    try:
        existing: list[str] = []
        if _USER_REGISTRY.exists():
            try:
                existing = json.loads(_USER_REGISTRY.read_text())
            except Exception:
                log.debug("User registry unreadable; starting a fresh list", exc_info=True)
        if username not in existing:
            existing.append(username)
            atomic_write_json(_USER_REGISTRY, existing)
    except Exception as e:
        log.warning("User registry update failed for %s: %s", username, e)
