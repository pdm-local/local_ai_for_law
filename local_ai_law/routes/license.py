"""
License management endpoints (admin only).

GET  /api/license/status              → lock state + licensing enabled flag
POST /api/license/import/{username}   → import a signed license for a user
GET  /api/license/user/{username}     → check license validity for a user
DELETE /api/license/user/{username}   → revoke user license
GET  /api/license/machine_id          → local machine fingerprint
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from local_ai_law.models import LicenseImportIn
from local_ai_law.routes.auth import require_admin
from local_ai_law.services.audit import audit_log
from local_ai_law.services.license import (
    get_lock_status, load_user_license, save_user_license,
    get_machine_id, is_licensing_enabled, USER_LICENSES_DIR,
)

router = APIRouter(prefix="/api/license", tags=["license"])


@router.get("/status")
async def license_status(_admin: dict = Depends(require_admin)):
    """GET (admin) — instance lock state and whether licensing is enabled."""
    return get_lock_status()


@router.post("/import/{username}")
async def import_license(
    username: str,
    body: LicenseImportIn,
    admin: dict = Depends(require_admin),
):
    """POST (admin) — import and validate a signed license for a user; 400 if rejected."""
    try:
        payload = save_user_license(username, body.license_json)
    except ValueError as e:
        raise HTTPException(400, f"License rejected: {e}") from e

    audit_log(
        admin["sub"], "license_import",
        target=username,
        details=f"expires={payload.get('expires_at')} edition={payload.get('edition')}",
    )
    return {
        "message": f"License saved for {username}",
        "expires_at": payload.get("expires_at"),
        "edition": payload.get("edition"),
        "modules": payload.get("modules"),
    }


@router.get("/user/{username}")
async def check_user_license(
    username: str,
    _admin: dict = Depends(require_admin),
):
    """GET (admin) — validity + metadata of a user's license (valid:false if missing/expired)."""
    payload = load_user_license(username)
    if payload is None:
        return {"valid": False, "username": username, "reason": "no_license_or_expired"}
    return {
        "valid": True,
        "username": username,
        "expires_at": payload.get("expires_at"),
        "edition": payload.get("edition"),
        "modules": payload.get("modules"),
        "role": payload.get("role"),
    }


@router.delete("/user/{username}")
async def revoke_license(
    username: str,
    admin: dict = Depends(require_admin),
):
    """DELETE (admin) — revoke (delete) a user's license file; 404 if none."""
    from local_ai_law.config import validate_safe_id
    try:
        validate_safe_id(username, "username")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    lic_file = USER_LICENSES_DIR / f"{username}.lic"
    if not lic_file.exists():
        raise HTTPException(404, "No license found for this user")
    lic_file.unlink()
    audit_log(admin["sub"], "license_revoke", target=username)
    return {"message": f"License revoked for {username}"}


@router.get("/machine_id")
async def machine_id(_admin: dict = Depends(require_admin)):
    """GET (admin) — this machine's licensing fingerprint and the licensing-enabled flag."""
    return {
        "machine_id": get_machine_id(),
        "licensing_enabled": is_licensing_enabled(),
    }
