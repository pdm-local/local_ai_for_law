"""Finder mode — 'show me where to look', not 'answer for me'.

A deliberately simple, LLM-free surface: the lawyer types a question and gets a ranked list of
DOCUMENTS + the exact SECTION(S) to read, with a source snippet — never a generated answer. It
reuses the strong half of the stack (`find_pointers` → `retrieve_carded`: matter-anchor,
entity-join, per-doc floor) and skips the weak/slow/risky half (cross-doc synthesis). No
hallucination risk: the lawyer reads the source, the tool only points. Per-user isolation is
enforced exactly as everywhere else — group/collection are resolved against the token's user.

Endpoints:
  GET  /finder      → the standalone finder page (static/finder.html)
  POST /api/find    → {query, group_id, max_docs?} → {pointers: [...], count}
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from local_ai_law.config import BASE_DIR, load_settings, get_group, log
from local_ai_law.models import FindIn
from local_ai_law.routes.auth import get_current_username
from local_ai_law.services.indexer import get_group_collection
from local_ai_law.services.retrieval import find_pointers

router = APIRouter()


@router.get("/finder")
async def finder_page():
    """Serve the standalone finder UI."""
    return FileResponse(BASE_DIR / "static" / "finder.html")


@router.post("/api/find")
async def api_find(body: FindIn, username: str = Depends(get_current_username)):
    """Retrieval-only pointer search. Returns ranked {source,title,matter,sections,score,
    snippet} for the user's own group — no LLM, no answer generation."""
    group = get_group(body.group_id, username)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    try:
        col = get_group_collection(body.group_id, username)
    except Exception as e:
        log.warning("finder: collection load failed for %s: %s", body.group_id, e)
        col = None
    if col is None:
        raise HTTPException(status_code=409, detail="Group is not indexed yet")

    cfg = load_settings(username)
    em = cfg["embed_model"]
    try:
        pointers = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: find_pointers(body.query, col, em, username, body.group_id,
                                  settings=cfg, max_docs=body.max_docs or 8),
        )
    except Exception as e:
        log.warning("finder: retrieval failed: %s", e)
        raise HTTPException(status_code=500, detail="Search failed") from e
    return {"pointers": pointers, "count": len(pointers)}
