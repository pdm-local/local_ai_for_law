"""
First-launch setup wizard endpoints.

GET  /api/setup/status           → {setup_complete, locked, edition, current_step}
GET  /api/setup/editions         → list of available editions with presets
POST /api/setup/complete         → finalize setup, write instance.json + edition.json
POST /api/setup/unlock           → submit signed operator recovery token to clear lockout
POST /api/setup/reset            → admin-only: wipe instance config and restart wizard
GET  /api/setup/machine_id       → return local machine fingerprint (for license generation)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Depends, Request

from local_ai_law.config import (
    DATA_DIR, save_instance, save_edition_config, load_instance,
    is_setup_complete, log,
    DEFAULT_SETTINGS,
)
from local_ai_law.models import SetupComplete, UnlockIn
from local_ai_law.routes.auth import require_admin
from local_ai_law.services.editions import edition_list, enabled_modules, wiki_page_types, resolve_edition
from local_ai_law.services.license import (
    get_lock_status, is_locked, unlock_with_token,
    get_machine_id, is_licensing_enabled,
)

router = APIRouter(prefix="/api/setup", tags=["setup"])


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def setup_status():
    inst = load_instance()
    lock = get_lock_status()
    return {
        "setup_complete":    inst.get("setup_complete", False),
        "locked":            lock["locked"],
        "edition":           inst.get("edition", ""),
        "org_name":          inst.get("org_name", ""),
        "setup_version":     inst.get("setup_version", 0),
        "licensing_enabled": is_licensing_enabled(),
    }


# ── Edition list for wizard ───────────────────────────────────────────────────

@router.get("/editions")
async def list_editions():
    return {"editions": edition_list()}


# ── Machine ID for operator (license generation) ──────────────────────────────

@router.get("/machine_id")
async def machine_id(request: Request):
    # Pre-setup the auth middleware is inactive, so this would otherwise leak the
    # hardware fingerprint to anyone who can reach the port. First-run licensing
    # happens on the box itself — restrict to loopback clients.
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(403, "machine_id is only available from the local machine")
    return {"machine_id": get_machine_id()}


# ── Complete setup ────────────────────────────────────────────────────────────

@router.post("/complete")
async def complete_setup(body: SetupComplete, request: Request):
    """
    Finalize the setup wizard.
    Creates the admin user, writes instance.json and edition.json.
    Safe to call only once; returns 409 if already set up.
    """
    if is_setup_complete():
        raise HTTPException(409, "Setup already complete. Use the admin panel to change settings.")

    if is_locked():
        raise HTTPException(423, "Instance is locked. Use /api/setup/unlock first.")

    # Register admin user
    from local_ai_law.services.auth import register_user, user_count
    if user_count() == 0:
        result = register_user(
            body.admin.username,
            body.admin.password,
            role="admin",
        )
        if not result:
            raise HTTPException(409, "Admin username already taken")
    else:
        # Users exist (rare edge case) — require existing admin to call this
        # For safety, refuse if users already exist without an auth check
        raise HTTPException(409, "Users already exist. Setup cannot be re-run.")

    ed_key     = body.edition.edition
    preset_key = body.edition.preset
    ed         = resolve_edition(ed_key, preset_key)
    modules    = list(enabled_modules(ed_key, preset_key))
    page_types = list(wiki_page_types(ed_key, preset_key))

    security = body.security
    sens_enforcement = security.sensitivity_enforcement if security else False

    # Write instance.json
    now = datetime.now(timezone.utc).isoformat()
    instance_data = {
        "org_name":       body.edition.org_name,
        "edition":        ed_key,
        "preset":         preset_key,
        "language":       body.edition.language,
        "jurisdiction":   body.edition.jurisdiction or "",
        "setup_complete": True,
        "setup_version":  1,
        "created_at":     now,
        "chat_model":     body.models.chat_model,
        "embed_model":    body.models.embed_model,
        "wiki_model":     body.models.wiki_model or body.models.chat_model,
        "relation_model": body.models.relation_model or body.models.chat_model,
        "sensitivity_enforcement": sens_enforcement,
        "machine_binding": security.machine_binding if security else False,
        "lock_attempts":  security.lock_attempts if security else 3,
    }
    save_instance(instance_data)

    # Write edition.json
    edition_data = {
        "edition":             ed_key,
        "preset":              preset_key,
        "enabled_modules":     modules,
        "wiki_page_types":     page_types,
        "wiki_first_retrieval": True,
        "wiki_enabled":        True,
        "graph_enabled":       True,
        "save_back_enabled":   "save_back" in modules,
        "graph_depth":         1,
        "sensitivity_enforcement": sens_enforcement,
        "answer_hint":         ed.answer_hint,
        "branding":            dict(ed.branding),
        "disclaimer":          ed.disclaimer,
    }
    save_edition_config(edition_data)

    # Write global settings.json with model choices + edition defaults
    from local_ai_law.config import SETTINGS_F, atomic_write_json
    gs = DEFAULT_SETTINGS.copy()
    gs.update(ed.default_settings)
    if ed.default_system_prompt:
        gs["system_prompt"] = ed.default_system_prompt
    gs.update({
        "chat_model":    body.models.chat_model,
        "embed_model":   body.models.embed_model,
        "wiki_model":    body.models.wiki_model or body.models.chat_model,
        "relation_model": body.models.relation_model or body.models.chat_model,
        "sensitivity_enforcement": sens_enforcement,
        "edition":       ed_key,
    })
    atomic_write_json(SETTINGS_F, gs)

    from local_ai_law.services.audit import audit_log
    ip = request.client.host if request.client else ""
    audit_log(body.admin.username, "setup_complete",
              target=ed_key, details=body.edition.org_name, ip=ip)

    log.info("Setup complete: org=%s edition=%s preset=%s",
             body.edition.org_name, ed_key, preset_key)

    return {
        "message":    "Setup complete",
        "edition":    ed_key,
        "org_name":   body.edition.org_name,
        "modules":    modules,
        "page_types": page_types,
    }


# ── Unlock (signed recovery token) ───────────────────────────────────────────

@router.post("/unlock")
async def unlock_instance(body: UnlockIn, request: Request):
    """
    Submit a signed operator recovery token to clear the lockout.
    The token is an Ed25519-signed JSON with type="unlock" and a future expiry.
    """
    if not is_locked():
        return {"message": "Instance is not locked"}

    success = unlock_with_token(body.token_json)
    if not success:
        from local_ai_law.services.license import record_failed_attempt
        state = record_failed_attempt()
        raise HTTPException(
            403,
            f"Invalid recovery token. "
            f"Attempts: {state['failed_attempts']}/{state['max_attempts']}",
        )

    from local_ai_law.services.audit import audit_log
    ip = request.client.host if request.client else ""
    audit_log("system", "instance_unlocked", ip=ip)
    return {"message": "Instance unlocked successfully"}


# ── Admin reset (requires existing admin JWT) ─────────────────────────────────

@router.post("/reset")
async def reset_setup(request: Request, admin: dict = Depends(require_admin)):
    """
    Admin-only: wipe instance.json and edition.json to re-run the wizard.
    Does NOT delete user data or documents.
    """
    for f in (DATA_DIR / "instance.json", DATA_DIR / "edition.json"):
        try:
            if f.exists():
                f.unlink()
        except Exception as e:
            log.error("Failed to remove %s during setup reset: %s", f, e)

    from local_ai_law.services.audit import audit_log
    ip = request.client.host if request.client else ""
    audit_log(admin["sub"], "setup_reset", ip=ip)
    log.warning("Setup reset by admin %s", admin["sub"])

    return {"message": "Instance configuration reset. Setup wizard will appear on next load."}
