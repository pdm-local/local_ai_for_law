"""
Job status and ingestion pipeline endpoints.

GET  /api/jobs/{job_id}                 → job status + progress
GET  /api/jobs?group_id=&status=        → list jobs for current user
POST /api/jobs/{job_id}/cancel          → request cancellation
POST /api/jobs/{job_id}/retry           → retry supported failed jobs
POST /api/ingestion/start               → start normalize+wiki+graph pipeline for docs
GET  /api/ingestion/doc_states?group_id= → per-doc pipeline state
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from local_ai_law.config import load_settings, group_doc_paths, get_group, load_edition_config, DEFAULT_LANGUAGE
from local_ai_law.models import IngestionJobIn
from local_ai_law.routes.auth import get_current_username, require_auth
from local_ai_law.services.audit import audit_log
from local_ai_law.services.jobs import (
    get_job, list_jobs, create_job, run_job_async, request_cancel, submit_job,
    update_job, JobContext, get_pool_status,
)
from local_ai_law.services.graph import get_doc_state, list_doc_states, set_doc_state

router = APIRouter(tags=["jobs"])


# ── Queue status ──────────────────────────────────────────────────────────────

@router.get("/api/jobs/queue-status")
async def queue_status(username: str = Depends(get_current_username)):
    """GET — job-pool status (active/queued per pool) and the Ollama concurrency limit."""
    status = get_pool_status()
    cfg = load_settings(username)
    from local_ai_law.services.ollama_client import generation_concurrency
    status["ollama_chat_concurrency"] = generation_concurrency()
    status["configured_ollama_chat_concurrency"] = cfg.get("ollama_chat_concurrency")
    return status


# ── Job status ────────────────────────────────────────────────────────────────

@router.get("/api/jobs/{job_id}")
async def get_job_status(
    job_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — one job's status/progress; only the owner (or an admin) may view it."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    # Users may only see their own jobs
    if job["username"] != username and user.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    return job


@router.get("/api/jobs")
async def list_user_jobs(
    group_id: str = Query(""),
    status: str = Query(""),
    limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — list jobs (filter by group/status); admins see all, users see only theirs."""
    # Admins can see all jobs; users see only their own
    query_username = "" if user.get("role") == "admin" else username
    jobs = list_jobs(username=query_username, group_id=group_id, status=status, limit=limit)
    return {"jobs": jobs}


@router.post("/api/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """POST — request cancellation of a pending/running job (owner or admin only)."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["username"] != username and user.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(400, f"Cannot cancel job in status '{job['status']}'")
    request_cancel(job_id)
    update_job(job_id, message="Cancellation requested...")
    return {"message": "Cancellation requested"}


@router.post("/api/jobs/{job_id}/retry")
async def retry_job(
    job_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """POST — re-queue a failed/cancelled/interrupted group-scoped job (owner or admin).
    Supported types: index_group, overview_refresh, contradictions, rebuild."""
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["username"] != username and user.get("role") != "admin":
        raise HTTPException(403, "Access denied")
    if job["status"] not in ("failed", "cancelled", "interrupted"):
        raise HTTPException(400, f"Cannot retry job in status '{job['status']}'")

    group_id = job.get("group_id") or ""
    job_type = job.get("type") or ""
    if not group_id:
        raise HTTPException(400, "Only group-scoped jobs can be retried")

    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")

    supported = {
        "index_group", "overview_refresh", "contradictions", "rebuild",
    }
    if job_type not in supported:
        raise HTTPException(400, f"Retry is not supported for job type '{job_type}'")

    if job_type == "rebuild":
        # Re-run the ingestion pipeline for the whole group; the per-doc state skip
        # logic resumes where the failed run stopped (force_rebuild stays off).
        audit_log(user["sub"], "job_retry", target=group_id, details=f"from={job_id}, type=rebuild")
        return await start_ingestion(IngestionJobIn(group_id=group_id), user=user, username=username)

    new_job_id = create_job(job_type, username, group_id, job.get("doc_name") or "", priority=job.get("priority") or "LOW")

    if job_type == "index_group":
        from local_ai_law.services.indexer import do_index_group
        submit_job(new_job_id, lambda ctx: do_index_group(group_id, username, False), priority="LOW")
    elif job_type == "overview_refresh":
        from local_ai_law.services.indexer import get_group_collection
        from local_ai_law.services.overview import generate_group_overview

        def _worker(ctx):
            cfg = load_settings(username)
            col = get_group_collection(group_id, username)
            paths = group_doc_paths(group_id, group, username)
            generate_group_overview(group_id, username, paths, col, cfg)
            return {"refreshed": True}

        submit_job(new_job_id, _worker, priority="LOW")
    elif job_type == "contradictions":
        from local_ai_law.services.indexer import get_group_collection
        from local_ai_law.services.contradictions import detect_contradictions

        def _worker(ctx):
            col = get_group_collection(group_id, username)
            detect_contradictions(username, group_id, col)
            return {"detected": True}

        submit_job(new_job_id, _worker, priority="LOW")
    audit_log(user["sub"], "job_retry", target=new_job_id, details=f"from={job_id}, type={job_type}")
    return {"job_id": new_job_id, "message": "Retry queued"}


# ── Ingestion pipeline ────────────────────────────────────────────────────────

@router.post("/api/ingestion/start")
async def start_ingestion(
    body: IngestionJobIn,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """
    Start the normalize + wiki + graph pipeline for one or more documents.
    Returns a job_id immediately; check /api/jobs/{job_id} for progress.
    If doc_names is empty, processes all documents in the group.
    """
    from local_ai_law.config import group_doc_paths, get_group, group_upload_dir

    edition_cfg = load_edition_config()
    wiki_enabled  = edition_cfg.get("wiki_enabled",  True) and body.run_wiki
    graph_enabled = edition_cfg.get("graph_enabled", True) and body.run_graph

    group = get_group(body.group_id, username)
    if not group:
        raise HTTPException(404, f"Group '{body.group_id}' not found")

    all_paths = group_doc_paths(body.group_id, group, username)
    _base = group_upload_dir(body.group_id, username)

    def _rel(p):
        try:
            return p.relative_to(_base).as_posix()
        except Exception:
            return p.name

    if body.doc_names:
        want = set(body.doc_names)
        # Identify documents by relative path (correct for nested/duplicate-named
        # files); fall back to basename for older callers that send bare filenames.
        paths = [p for p in all_paths if _rel(p) in want or p.name in want]
        if not paths:
            raise HTTPException(404, "None of the specified documents found in group")
    else:
        paths = all_paths

    if not paths:
        raise HTTPException(400, "No documents found to process")

    job_id = create_job("rebuild", username, body.group_id,
                        f"{len(paths)} docs")

    def _worker(ctx: JobContext) -> dict:
        from local_ai_law.services.extraction import extract_file
        from local_ai_law.services.normalizer import normalize_doc
        from local_ai_law.services.wiki import generate_wiki_page, extract_and_store_relations
        settings = load_settings(username)
        processed = []

        for doc_path in paths:
            if ctx.check_cancel():
                break
            # Group-relative path is the document identity (doc-state key, normalized
            # artifact slug, wiki/graph source) — basenames collide across folders.
            doc_name = _rel(doc_path)

            # Check if already done (skip unless force_rebuild)
            state = get_doc_state(username, body.group_id, doc_name)
            skip_norm  = state.get("normalize_ok") and not body.force_rebuild
            skip_wiki  = state.get("wiki_ok")  and not body.force_rebuild
            skip_graph = state.get("graph_ok") and not body.force_rebuild

            # 1. Normalize
            if body.run_normalize and not skip_norm:
                ctx.progress(f"Normalizing {doc_name}...")
                try:
                    extracted = extract_file(doc_path)
                    normalize_doc(extracted, doc_name, username, body.group_id)
                    set_doc_state(username, body.group_id, doc_name, normalize_ok=True)
                except Exception as e:
                    ctx.progress(f"Normalize failed for {doc_name}: {e}")

            # 2. Wiki generation (one topic per doc = doc title)
            if wiki_enabled and not skip_wiki:
                from local_ai_law.services.normalizer import load_normalized
                norm_text = load_normalized(username, body.group_id, doc_name)
                if norm_text:
                    ctx.progress(f"Generating wiki for {doc_name}...")
                    try:
                        # Determine page_type from edition
                        page_types = edition_cfg.get("wiki_page_types", ["topics"])
                        page_type = page_types[0] if page_types else "topics"
                        import re
                        topic = re.sub(r"\.[^.]+$", "", doc_name).replace("-", " ").replace("_", " ").title()
                        group_obj = get_group(body.group_id, username)
                        lang = (group_obj or {}).get("language") or settings.get("app_language") or DEFAULT_LANGUAGE
                        generate_wiki_page(
                            username=username,
                            group_id=body.group_id,
                            page_type=page_type,
                            topic=topic,
                            normalized_text=norm_text,
                            source_docs=[doc_name],
                            settings=settings,
                            language=lang,
                        )
                        set_doc_state(username, body.group_id, doc_name, wiki_ok=True)
                    except Exception as e:
                        ctx.progress(f"Wiki gen failed for {doc_name}: {e}")

            # 3. Graph relation extraction
            if graph_enabled and not skip_graph:
                from local_ai_law.services.normalizer import load_normalized
                norm_text = load_normalized(username, body.group_id, doc_name)
                if norm_text:
                    ctx.progress(f"Extracting relations for {doc_name}...")
                    try:
                        extract_and_store_relations(
                            username, body.group_id, doc_name,
                            norm_text[:4000], settings,
                        )
                        set_doc_state(username, body.group_id, doc_name, graph_ok=True)
                    except Exception as e:
                        ctx.progress(f"Graph extraction failed for {doc_name}: {e}")

            processed.append(doc_name)
            ctx.progress(f"Done: {doc_name}", step=1)

        return {"processed": processed, "total": len(paths)}

    run_job_async(job_id, _worker, total_items=len(paths))
    audit_log(user["sub"], "ingestion_start", target=body.group_id,
              details=f"{len(paths)} docs, wiki={wiki_enabled}, graph={graph_enabled}")

    return {
        "job_id":    job_id,
        "message":   f"Ingestion pipeline started for {len(paths)} document(s)",
        "doc_count": len(paths),
    }


@router.get("/api/ingestion/doc_states")
async def doc_pipeline_states(
    group_id: str = Query(...),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — per-document pipeline state (normalize/wiki/graph ok flags) for a group."""
    states = list_doc_states(username, group_id)
    return {"group_id": group_id, "states": states}
