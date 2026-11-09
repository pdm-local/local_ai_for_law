"""
Wiki browser and generation endpoints.

GET  /api/wiki/pages?group_id=&page_type=         → list pages
GET  /api/wiki/page/{page_id}                      → read page (metadata + body)
GET  /api/wiki/page/{page_id}/evidence             → section evidence refs
GET  /api/wiki/search?q=&group_id=                 → FTS5 search
POST /api/wiki/generate                            → queue wiki generation job
PUT  /api/wiki/page/{page_id}                      → update page body / metadata
DELETE /api/wiki/page/{page_id}                    → delete page
POST /api/wiki/save_back                           → save an answer snippet as a wiki page

Sensitivity access:
  - normal / internal: all authenticated users
  - privileged: admin or user with explicit role (Law Firm / Healthcare editions)
  - sensitive: admin only

Module guard: "wiki" module must be enabled.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from local_ai_law.config import load_settings, load_edition_config, atomic_write_text
from local_ai_law.models import WikiGenerateIn, WikiPageUpdateIn, SaveBackIn
from local_ai_law.routes.auth import get_current_username, require_auth
from local_ai_law.services.audit import audit_log
from local_ai_law.services.graph import (
    list_wiki_pages, get_wiki_page, delete_wiki_page, search_wiki_fts,
)
from local_ai_law.services.wiki import (
    read_wiki_page, update_wiki_page, delete_page_file,
    generate_wiki_page,
)

router = APIRouter(prefix="/api/wiki", tags=["wiki"])

_SENSITIVITY_ORDER = {"normal": 0, "internal": 1, "privileged": 2, "sensitive": 3}


# ── Module guard helper ───────────────────────────────────────────────────────

def _require_wiki(edition_cfg: dict):
    modules = edition_cfg.get("enabled_modules", [])
    if modules and "wiki" not in modules:
        raise HTTPException(403, "Wiki module is not enabled for this edition")


def _sensitivity_ceiling(user: dict) -> str:
    """Max sensitivity level the user may access."""
    edition_cfg = load_edition_config()
    if not edition_cfg.get("sensitivity_enforcement", False):
        return "sensitive"  # no enforcement → full access
    role = user.get("role", "user")
    if role == "admin":
        return "sensitive"
    return "internal"  # regular users see only normal + internal


# ── List pages ────────────────────────────────────────────────────────────────

@router.get("/pages")
async def list_pages(
    group_id: str = Query(...),
    page_type: str = Query(""),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — list a group's wiki pages (filtered by type + the user's sensitivity ceiling),
    each with a short plaintext excerpt."""
    edition_cfg = load_edition_config()
    _require_wiki(edition_cfg)
    ceiling = _sensitivity_ceiling(user)
    pages = list_wiki_pages(username, group_id, page_type=page_type, sensitivity_max=ceiling)
    for p in pages:
        page_data = read_wiki_page(username, group_id, p["page_type"], p["slug"])
        body = (page_data or {}).get("body", "") or ""
        # strip markdown headings/bullets for clean excerpt
        lines = [l.lstrip("#>*- ").strip() for l in body.splitlines() if l.strip() and not l.startswith("```")]
        excerpt = " ".join(lines)[:200]
        p["excerpt"] = excerpt
    return {"pages": pages, "count": len(pages)}


# ── Read page ─────────────────────────────────────────────────────────────────

@router.get("/page/{page_id}")
async def read_page(
    page_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — read one wiki page (metadata + body); enforces the sensitivity clearance."""
    edition_cfg = load_edition_config()
    _require_wiki(edition_cfg)

    meta = get_wiki_page(username, page_id)
    if not meta:
        raise HTTPException(404, "Wiki page not found")

    ceiling = _sensitivity_ceiling(user)
    if _SENSITIVITY_ORDER.get(meta.get("sensitivity", "normal"), 0) > _SENSITIVITY_ORDER.get(ceiling, 3):
        raise HTTPException(403, "Access denied: insufficient clearance for this page")

    page = read_wiki_page(username, meta["group_id"], meta["page_type"], meta["slug"])
    if not page:
        raise HTTPException(404, "Wiki page file not found")

    audit_log(user["sub"], "wiki_read", target=page_id, details=meta.get("title", ""))
    return page


# ── Section evidence ──────────────────────────────────────────────────────────

@router.get("/page/{page_id}/evidence")
async def get_evidence(
    page_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — per-section evidence (source chunk refs) backing a wiki page."""
    meta = get_wiki_page(username, page_id)
    if not meta:
        raise HTTPException(404, "Wiki page not found")

    ceiling = _sensitivity_ceiling(user)
    if _SENSITIVITY_ORDER.get(meta.get("sensitivity", "normal"), 0) > _SENSITIVITY_ORDER.get(ceiling, 3):
        raise HTTPException(403, "Access denied")

    from local_ai_law.services.wiki import read_page_evidence
    evidence = read_page_evidence(username, meta["group_id"], meta["page_type"], meta["slug"])
    return {"page_id": page_id, "evidence": evidence}


# ── FTS5 search ───────────────────────────────────────────────────────────────

@router.get("/search")
async def search_wiki(
    q: str = Query(..., min_length=1),
    group_id: str = Query(...),
    limit: int = Query(10, ge=1, le=50),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """GET — FTS5 full-text search over the group's wiki pages (clearance-filtered)."""
    edition_cfg = load_edition_config()
    _require_wiki(edition_cfg)
    ceiling = _sensitivity_ceiling(user)
    hits = search_wiki_fts(username, group_id, q, limit=limit, sensitivity_max=ceiling)
    return {"query": q, "results": hits, "count": len(hits)}


# ── Generate wiki page (queued job) ──────────────────────────────────────────

@router.post("/generate")
async def generate_page(
    body: WikiGenerateIn,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """POST — queue a wiki-generation job over the named docs; returns a job_id."""
    edition_cfg = load_edition_config()
    _require_wiki(edition_cfg)

    # Validate page_type against edition
    allowed_types = edition_cfg.get("wiki_page_types", [])
    if allowed_types and body.page_type not in allowed_types:
        raise HTTPException(
            400,
            f"Page type '{body.page_type}' is not available for this edition. "
            f"Allowed: {allowed_types}",
        )

    from local_ai_law.services.jobs import create_job, run_job_async, JobContext
    from local_ai_law.services.normalizer import load_normalized

    job_id = create_job("wiki_build", username, body.group_id, body.topic)

    def _worker(ctx: JobContext) -> dict:
        settings = load_settings(username)
        ctx.progress("Loading source documents...", step=0)

        # Aggregate normalized text from all specified docs
        combined_text = ""
        for doc_name in body.doc_names:
            norm = load_normalized(username, body.group_id, doc_name)
            if norm:
                combined_text += f"\n\n{norm}"
            else:
                combined_text += f"\n\n(Document {doc_name} not yet normalized — run ingestion first)"
            ctx.progress(f"Loaded {doc_name}")

        ctx.progress("Generating wiki page...")
        result = generate_wiki_page(
            username=username,
            group_id=body.group_id,
            page_type=body.page_type,
            topic=body.topic,
            normalized_text=combined_text.strip(),
            source_docs=body.doc_names,
            settings=settings,
            sensitivity=body.sensitivity,
            tags=body.tags,
            language=body.language,
        )
        return result

    run_job_async(job_id, _worker, total_items=len(body.doc_names) + 1)
    audit_log(user["sub"], "wiki_generate", target=body.group_id, details=body.topic)

    return {"job_id": job_id, "message": "Wiki generation queued"}


# ── Update page ───────────────────────────────────────────────────────────────

@router.put("/page/{page_id}")
async def update_page(
    page_id: str,
    body: WikiPageUpdateIn,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """PUT — update a wiki page's body and (admin-only) sensitivity/tags."""
    meta = get_wiki_page(username, page_id)
    if not meta:
        raise HTTPException(404, "Wiki page not found")

    ceiling = _sensitivity_ceiling(user)
    if _SENSITIVITY_ORDER.get(meta.get("sensitivity", "normal"), 0) > _SENSITIVITY_ORDER.get(ceiling, 3):
        raise HTTPException(403, "Access denied: insufficient clearance")

    meta_updates = {}
    if body.sensitivity is not None:
        # Only admin can change sensitivity
        if user.get("role") != "admin" and body.sensitivity != meta.get("sensitivity"):
            raise HTTPException(403, "Only admins can change page sensitivity")
        meta_updates["sensitivity"] = body.sensitivity
    if body.tags is not None:
        meta_updates["tags"] = body.tags

    update_wiki_page(
        username, meta["group_id"], meta["page_type"], meta["slug"],
        body.body, meta_updates or None,
    )
    audit_log(user["sub"], "wiki_update", target=page_id, details=meta.get("title", ""))
    return {"message": "Page updated"}


# ── Delete page ───────────────────────────────────────────────────────────────

@router.delete("/page/{page_id}")
async def delete_page(
    page_id: str,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """DELETE (admin) — remove a wiki page (registry row + file)."""
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required to delete wiki pages")

    meta = get_wiki_page(username, page_id)
    if not meta:
        raise HTTPException(404, "Wiki page not found")

    delete_wiki_page(username, page_id)
    delete_page_file(username, meta["group_id"], meta["page_type"], meta["slug"])
    audit_log(user["sub"], "wiki_delete", target=page_id, details=meta.get("title", ""))
    return {"message": "Page deleted"}


# ── Save-back (answer → wiki page) ───────────────────────────────────────────

@router.post("/save_back")
async def save_back(
    body: SaveBackIn,
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """POST — save a chat answer snippet as a new internal wiki page (if save-back enabled)."""
    edition_cfg = load_edition_config()
    if not edition_cfg.get("save_back_enabled", False):
        raise HTTPException(403, "Save-back is not enabled for this edition")

    from local_ai_law.services.wiki import wiki_page_path, _slug, _build_frontmatter
    from datetime import datetime, timezone

    slug = _slug(body.topic)
    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "title": body.topic,
        "page_type": body.page_type,
        "group": body.group_id,
        "source_docs": body.source_docs,
        "tags": body.tags,
        "created_at": now,
        "updated_at": now,
        "provenance": "user-saved",
        "visibility": "internal",
        "sensitivity": "internal",
        "confidence": 1.0,
    }
    content = _build_frontmatter(meta) + f"\n\n{body.content.strip()}\n"
    page_type = body.page_type or "topics"
    path = wiki_page_path(username, body.group_id, page_type, slug)
    atomic_write_text(path, content, encoding="utf-8")

    from local_ai_law.services.graph import upsert_wiki_page as reg
    pid = reg(username, {
        **meta, "slug": slug, "file_path": str(path),
        "body": body.content[:2000],
    })

    audit_log(user["sub"], "wiki_save_back", target=body.group_id, details=body.topic)
    return {"message": "Saved to wiki", "page_id": pid, "slug": slug}
