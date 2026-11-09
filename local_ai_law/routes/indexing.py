"""Index trigger + status endpoints  -  per-user data isolation."""

from fastapi import APIRouter, Depends, HTTPException

from local_ai_law.config import get_group, group_doc_paths, load_settings
from local_ai_law.routes.auth import get_current_username
from local_ai_law.services.audit import audit_log
from local_ai_law.services.indexer import idx_status, do_index_group, get_group_collection
from local_ai_law.services.overview import load_overview, generate_group_overview
from local_ai_law.services.contradictions import detect_contradictions
from local_ai_law.services.jobs import create_job, submit_job

router = APIRouter(prefix="/api", tags=["indexing"])


@router.post("/groups/{group_id}/index")
async def api_group_index(
    group_id: str,
    reset: bool = False,
    username: str = Depends(get_current_username),
):
    """POST — start indexing a group (LOW-pool job). `reset=true` re-embeds from
    scratch; otherwise only new/changed files. 409 if already indexing."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    st = idx_status(group_id, username)
    if st.is_indexing:
        raise HTTPException(409, "Indexing already running for this group")
    job_id = create_job("index_group", username, group_id)
    submit_job(job_id, lambda ctx: do_index_group(group_id, username, reset), priority="LOW")
    audit_log(username, "index_group", target=group_id, details=f"reset={reset}")
    return {"message": "Indexing started", "reset": reset, "job_id": job_id}


@router.post("/groups/{group_id}/index/cancel")
async def api_cancel_index(group_id: str, username: str = Depends(get_current_username)):
    """POST — request cancellation of an in-progress index (finishes the current file)."""
    st = idx_status(group_id, username)
    with st._lock:
        if not st.is_indexing:
            raise HTTPException(409, "Not currently indexing")
        st.cancel_requested = True
    return {"message": "Cancel requested"}


@router.get("/groups/{group_id}/index/status")
async def api_group_idx_status(group_id: str, username: str = Depends(get_current_username)):
    """GET — current indexing status/progress plus the active chunk/embed config."""
    status = idx_status(group_id, username).to_dict()
    cfg = load_settings(username)
    status["index_config"] = {
        "chunk_size": cfg.get("chunk_size"),
        "chunk_overlap": cfg.get("chunk_overlap"),
        "embed_model": cfg.get("embed_model"),
    }
    return status


@router.get("/groups/{group_id}/overview")
async def api_group_overview(group_id: str, username: str = Depends(get_current_username)):
    """GET — the saved AI matter overview JSON for a group, or 404 if not generated."""
    ov = load_overview(group_id, username)
    if not ov:
        raise HTTPException(404, "No overview yet -- re-index the group to generate one.")
    return ov


@router.post("/groups/{group_id}/overview/refresh")
async def api_group_overview_refresh(
    group_id: str,
    username: str = Depends(get_current_username),
):
    """POST — regenerate the matter overview + per-document key ideas (LOW-pool job)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    st = idx_status(group_id, username)
    if st.is_indexing:
        raise HTTPException(409, "Indexing already in progress")

    def _refresh(ctx):
        cfg = load_settings(username)
        _col = get_group_collection(group_id, username)
        _paths = group_doc_paths(group_id, group, username)
        with st._lock:
            st.is_indexing = True
            st.phase = "Generating matter overview..."
        try:
            generate_group_overview(group_id, username, _paths, _col, cfg)
        finally:
            with st._lock:
                st.is_indexing = False
                st.phase = ""

    job_id = create_job("overview_refresh", username, group_id)
    submit_job(job_id, _refresh, priority="LOW")
    return {"message": "Overview refresh started", "job_id": job_id}


@router.post("/groups/{group_id}/matter-overview/rebuild")
async def api_rebuild_matter_overview(
    group_id: str,
    username: str = Depends(get_current_username),
):
    """Rebuild the MatterOverview wiki page from the existing overview JSON."""
    from local_ai_law.services.overview import load_overview, _write_matter_overview_wiki
    ov = load_overview(group_id, username)
    if not ov:
        raise HTTPException(404, "No overview yet - run the full overview refresh first.")
    _write_matter_overview_wiki(group_id, username, ov)
    return {"message": "Matter overview page rebuilt."}


# ── Contradiction detection ──────────────────────────────────────────────────

@router.post("/groups/{group_id}/contradictions/detect")
async def api_detect_contradictions(
    group_id: str,
    username: str = Depends(get_current_username),
):
    """POST — run cross-document contradiction detection over the indexed group (LOW-pool job)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    col = get_group_collection(group_id, username)
    if col.count() == 0:
        raise HTTPException(400, "No indexed content  -  index first")

    def _run(ctx):
        detect_contradictions(username, group_id, col)

    job_id = create_job("contradictions", username, group_id)
    submit_job(job_id, _run, priority="LOW")
    return {"status": "detecting", "message": "Contradiction detection started", "job_id": job_id}


@router.get("/groups/{group_id}/contradictions")
async def api_get_contradictions(group_id: str, username: str = Depends(get_current_username)):
    """GET — saved contradiction findings for a group (status not_run if never executed)."""
    from local_ai_law.services.contradictions import load_contradictions
    data = load_contradictions(username, group_id)
    if not data:
        return {"contradictions": [], "status": "not_run"}
    return data
