"""Admin endpoints  -  audit log, backup/restore, metrics, user management."""

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from local_ai_law.routes.auth import require_admin, get_current_username
from local_ai_law.services.audit import read_audit_log
from local_ai_law.services.backup import (
    RestoreBusy, create_backup, delete_backup, get_backup_file, list_backups, restore_backup,
)
from local_ai_law.services.metrics import get_metrics
from local_ai_law.services.auth import list_users

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ── Audit Log ────────────────────────────────────────────────────────────────

@router.get("/audit")
async def api_audit_log(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    user: str = Query(""),
    action: str = Query(""),
    _admin: dict = Depends(require_admin),
):
    """GET (admin) — paginated audit-log entries, optionally filtered by user/action."""
    entries = read_audit_log(limit=limit, offset=offset, user_filter=user, action_filter=action)
    return {"entries": entries, "limit": limit, "offset": offset}


# ── Backup / Restore ────────────────────────────────────────────────────────

@router.post("/backup")
async def api_create_backup(
    username: str = Depends(get_current_username),
    _admin: dict = Depends(require_admin),
):
    """POST (admin) — create a tarball backup of this user's data; returns its name/size."""
    try:
        path = create_backup(username)
        return {"message": "Backup created", "filename": path.name, "size": path.stat().st_size}
    except Exception as e:
        raise HTTPException(500, f"Backup failed: {e}") from e


@router.get("/backups")
async def api_list_backups(
    username: str = Depends(get_current_username),
    _admin: dict = Depends(require_admin),
):
    """GET (admin) — list available backups (name, size, timestamp)."""
    return {"backups": list_backups(username)}


@router.get("/backups/{filename}")
async def api_download_backup(
    filename: str,
    username: str = Depends(get_current_username),
    _admin: dict = Depends(require_admin),
):
    """GET (admin) — download a backup tarball by filename (validated), or 404."""
    path = get_backup_file(username, filename)
    if not path:
        raise HTTPException(404, "Backup not found")
    return FileResponse(str(path), filename=path.name, media_type="application/gzip")


@router.post("/backups/{filename}/restore")
async def api_restore_backup(
    filename: str,
    username: str = Depends(get_current_username),
    _admin: dict = Depends(require_admin),
):
    """POST (admin) — restore this user's data from a backup (stage-then-swap), or 400.

    409 while the user has running or queued jobs: they would write into the old data."""
    try:
        success = restore_backup(username, filename)
    except RestoreBusy as e:
        raise HTTPException(409, f"Restore waits for running work to finish: {e}.") from e
    if not success:
        raise HTTPException(400, "Restore failed  -  invalid or missing backup")
    return {"message": f"Restored from {filename}"}


@router.delete("/backups/{filename}")
async def api_delete_backup(
    filename: str,
    username: str = Depends(get_current_username),
    _admin: dict = Depends(require_admin),
):
    """DELETE (admin) — remove a backup file by name, or 404."""
    if not delete_backup(username, filename):
        raise HTTPException(404, "Backup not found")
    return {"message": "Backup deleted"}


# ── Metrics ──────────────────────────────────────────────────────────────────

@router.get("/metrics")
async def api_metrics(_admin: dict = Depends(require_admin)):
    """GET (admin) — system metrics (uptime, requests, CPU/RAM, Ollama, user count)."""
    return get_metrics()


# ── Users ────────────────────────────────────────────────────────────────────

@router.get("/users")
async def api_list_users(_admin: dict = Depends(require_admin)):
    """GET (admin) — list registered users (without password hashes)."""
    return {"users": list_users()}
