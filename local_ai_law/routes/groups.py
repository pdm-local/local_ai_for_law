"""Group CRUD endpoints  -  per-user data isolation."""

import asyncio
import re
import shutil
import uuid
from datetime import datetime

import chromadb
from fastapi import APIRouter, Depends, HTTPException, Response

from local_ai_law.config import (
    user_db_path, load_groups, save_groups, group_doc_paths, group_upload_dir, DEFAULT_LANGUAGE,
    load_settings, log,
)
from local_ai_law.models import GroupIn
from local_ai_law.routes.auth import get_current_username
from local_ai_law.services.audit import audit_log
from local_ai_law.services.indexer import (
    get_group_collection, safe_coll_name, idx_status,
)
from local_ai_law.services.overview import overview_path

router = APIRouter(prefix="/api", tags=["groups"])


@router.get("/groups")
async def api_list_groups(username: str = Depends(get_current_username)):
    """GET — all of the user's groups (matters) with doc/chunk counts, index status,
    and a short overview snippet for the card view. The counting reads the index, so it
    runs in a thread: on the event loop it held up every other request and the chat
    WebSocket for as long as it took (tens of seconds on a large matter set)."""
    return {"groups": await asyncio.to_thread(_list_groups, username)}


def _chunk_count(col, sources: list[str]) -> int:
    """Chunks the index holds for these files: ids only, a few hundred files per query
    (one query per file, with its text, was what made the matter list slow)."""
    n = 0
    for i in range(0, len(sources), 500):
        part = sources[i:i + 500]
        try:
            n += len(col.get(where={"source": {"$in": part}}, include=[]).get("ids", []))
        except Exception:
            log.debug("Chunk count failed for %d sources", len(part), exc_info=True)
    return n


def _list_groups(username: str) -> list[dict]:
    import json as _json
    groups = load_groups(username)
    result = []
    for g in groups:
        st = idx_status(g["id"], username)
        doc_paths = group_doc_paths(g["id"], g, username)
        coll_count = 0
        try:
            col = get_group_collection(g["id"], username)
            upload_base = group_upload_dir(g["id"], username)
            coll_count = _chunk_count(col, [p.relative_to(upload_base).as_posix() for p in doc_paths])
        except Exception:
            log.debug("Could not count chunks for group %s", g.get("id"), exc_info=True)
        # Load AI-generated overview snippet (first sentence of summary)
        overview_snippet = g.get("description", "")
        if not overview_snippet:
            try:
                ov_path = overview_path(g["id"], username)
                if ov_path.exists():
                    ov = _json.loads(ov_path.read_text())
                    summary = ov.get("summary", "")
                    # Take first two sentences as snippet
                    sentences = summary.replace("  ", " ").split(". ")
                    overview_snippet = ". ".join(sentences[:2]).strip()
                    if overview_snippet and not overview_snippet.endswith("."):
                        overview_snippet += "."
            except Exception:
                log.debug("Overview snippet load failed for group %s", g.get("id"), exc_info=True)
        result.append({
            **g,
            "doc_count":       len(doc_paths),
            "chunk_count":     coll_count,
            "is_indexing":     st.is_indexing,
            "last_indexed":    st.last_completed,
            "index_error":     st.error,
            "overview_snippet": overview_snippet,
        })
    return result


@router.post("/groups")
async def api_create_group(body: GroupIn, username: str = Depends(get_current_username)):
    """POST — create a new group (matter); defaults language to the app setting."""
    groups = load_groups(username)
    group_id = str(uuid.uuid4())[:8]
    app_lang = load_settings(username).get("app_language") or DEFAULT_LANGUAGE
    new_g = {
        "id":              group_id,
        "name":            body.name.strip(),
        "description":     body.description or "",
        "color":           body.color or "cat-1",
        "emoji":           body.emoji or "\U0001f4da",
        "language":        body.language or app_lang,
        "group_type":      "knowledge",
        "created":         datetime.now().isoformat(),
    }
    groups.append(new_g)
    save_groups(groups, username)
    group_upload_dir(group_id, username)
    audit_log(username, "create_group", target=group_id, details=body.name.strip())
    return new_g


@router.put("/groups/{group_id}")
async def api_update_group(group_id: str, body: GroupIn, username: str = Depends(get_current_username)):
    """PUT — update a group's name/description/color/emoji/language, or 404."""
    groups = load_groups(username)
    for i, g in enumerate(groups):
        if g["id"] == group_id:
            groups[i] = {
                **g,
                "name":            body.name.strip(),
                "description":     body.description or "",
                "color":           body.color or g["color"],
                "emoji":           body.emoji or g["emoji"],
                "language":        body.language or g.get("language", DEFAULT_LANGUAGE),
                "group_type":      "knowledge",
            }
            save_groups(groups, username)
            audit_log(username, "update_group", target=group_id)
            return groups[i]
    raise HTTPException(404, "Group not found")


@router.get("/groups/{group_id}/report")
async def api_group_report(group_id: str, username: str = Depends(get_current_username)):
    """GET — the matter report (overview + per-document summaries) as a download.

    PDF when the optional WeasyPrint extra is installed; otherwise the same report as a
    self-contained HTML file (escaped, no scripts, a CSP that loads nothing), which the
    reader prints to PDF from the browser. The default build has no WeasyPrint: its
    hyphenation dictionaries are GPL/LGPL. Generator: services/reports.py.
    """
    from local_ai_law.services.reports import generate_report_html, generate_report_pdf
    g = next((x for x in load_groups(username) if x["id"] == group_id), None)
    if not g:
        raise HTTPException(404, "Group not found")
    lang = g.get("language") or load_settings(username).get("app_language") or DEFAULT_LANGUAGE
    lang = "en" if str(lang).startswith("en") else "nl"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", g["name"]).strip("_") or "report"
    try:
        pdf = generate_report_pdf(username, group_id, g["name"], lang)
    except ImportError:
        html = generate_report_html(username, group_id, g["name"], lang)
        return Response(
            content=html.encode("utf-8"),
            media_type="text/html; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe}_report.html"',
                     "X-Content-Type-Options": "nosniff"},
        )
    except Exception as e:
        log.error("Report generation failed for %s/%s: %s", username, group_id, e)
        raise HTTPException(500, "Report generation failed.") from e
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe}_report.pdf"'},
    )


@router.delete("/groups/{group_id}")
async def api_delete_group(group_id: str, username: str = Depends(get_current_username)):
    """DELETE — remove a group and all its data: graph/wiki entries, the Chroma
    collection, and the uploaded files. Best-effort per store so one failure can't
    block the rest."""
    from local_ai_law.services.jobs import cancel_jobs_for_group
    from local_ai_law.services.graph import delete_group_graph, delete_group_wiki
    cancel_jobs_for_group(group_id)
    # True group delete: clear graph nodes/edges AND the wiki registry (graph
    # rebuilds clear only the graph — see delete_group_graph).
    try:
        delete_group_graph(username, group_id)
        delete_group_wiki(username, group_id)
    except Exception:
        log.debug("Graph/wiki cleanup failed for group %s", group_id, exc_info=True)
    groups = load_groups(username)
    groups = [g for g in groups if g["id"] != group_id]
    save_groups(groups, username)
    try:
        client = chromadb.PersistentClient(path=str(user_db_path(username)))
        client.delete_collection(safe_coll_name(group_id))
    except Exception:
        log.debug("No Chroma collection to delete for group %s", group_id, exc_info=True)
    ud = group_upload_dir(group_id, username)
    if ud.exists():
        shutil.rmtree(ud)
    ov = overview_path(group_id, username)
    if ov.exists():
        ov.unlink()
    audit_log(username, "delete_group", target=group_id)
    return {"message": "Group deleted"}
