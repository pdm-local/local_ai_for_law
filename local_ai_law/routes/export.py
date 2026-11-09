"""Group export — zip everything for a group into a portable archive."""
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from local_ai_law.config import (
    get_group, group_doc_paths, group_upload_dir, get_active_edition,
    log,
)
from local_ai_law.routes.auth import get_current_username, require_auth
from local_ai_law.services.graph import list_wiki_pages, _conn as graph_conn
from local_ai_law.services.wiki import read_wiki_page

router = APIRouter(prefix="/api/export", tags=["export"])


def _safe_name(name: str) -> str:
    """Sanitise a filename for use inside the ZIP."""
    return re.sub(r'[^\w.\-]', '_', name)


def _doc_slug(doc_name: str) -> str:
    """Stem of the document filename, safe for use as a JSON filename."""
    return _safe_name(Path(doc_name).stem)


@router.get("/group")
async def export_group(
    group_id: str = Query(...),
    user: dict = Depends(require_auth),
    username: str = Depends(get_current_username),
):
    """
    Stream a ZIP archive containing all generated data for a group.

    Structure:
      wiki/           — one JSON per wiki page
      summaries/      — AI summary per source document
      connections/    — knowledge graph concepts per source document
      annotations/    — highlights & notes per source document
    """
    grp = get_group(group_id, username)
    if not grp:
        raise HTTPException(404, "Group not found")

    group_name = _safe_name(grp.get("name", group_id))
    zip_name = f"{group_name}_export.zip"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:

        # ── manifest.json — provenance so an export is self-describing ─────────
        from local_ai_law.services.indexer import group_chunk_stats
        chunk_stats = group_chunk_stats(group_id, username)
        base = group_upload_dir(group_id, username)
        documents = []
        for p in group_doc_paths(group_id, grp, username):
            rel = p.relative_to(base).as_posix()
            documents.append({
                "rel_path": rel,
                "bytes": p.stat().st_size if p.exists() else 0,
                "chunks": chunk_stats.get(rel, 0),
            })
        manifest = {
            "app_edition":  get_active_edition(),
            "exported_at":  datetime.now(timezone.utc).isoformat(),
            "group_id":     group_id,
            "group_name":   grp.get("name", group_id),
            "document_count": len(documents),
            "documents":    documents,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))

        # ── wiki/ ─────────────────────────────────────────────────────────────
        wiki_pages = list_wiki_pages(username, group_id)
        for page in wiki_pages:
            page_type = page.get("page_type", "page")
            slug = page.get("slug") or _safe_name(page.get("title", page.get("id", "unknown")))
            full = read_wiki_page(username, group_id, page_type, slug)
            if full:
                fname = f"wiki/{page_type}__{slug}.json"
                zf.writestr(fname, json.dumps(full, indent=2, ensure_ascii=False))

        # ── per-document sections ─────────────────────────────────────────────
        # Read from the ACTUAL stores (not reconstructed slug paths): summaries
        # live in one group file keyed by relative path; annotations via the
        # annotations service; concepts from the real graph edges.
        from local_ai_law.services.doc_summary import load_doc_summaries
        from local_ai_law.services.annotations import list_group_annotations

        def _flat(name: str) -> str:
            return _safe_name(name.replace("/", "__")) or "doc"

        # summaries/ — AI doc summary (one entry per document, keyed by rel path)
        for doc_name, data in load_doc_summaries(group_id, username).items():
            if not isinstance(data, dict):
                continue
            try:
                zf.writestr(f"summaries/{_flat(doc_name)}.json",
                            json.dumps(data, indent=2, ensure_ascii=False))
            except Exception:
                log.debug("Skipped summary %s in export", doc_name, exc_info=True)

        # connections/ — Concept nodes linked to each document (direction-agnostic,
        # matches both HAS_KEYWORD (doc→concept) and legacy FOUND_IN (concept→doc)).
        try:
            c = graph_conn(username)
            doc_rows = c.execute(
                "SELECT id, source_path FROM nodes WHERE group_id=? AND type='Document'",
                (group_id,),
            ).fetchall()
            for d in doc_rows:
                src = d["source_path"] or ""
                rows = c.execute(
                    """
                    SELECT DISTINCT cn.id, cn.type, cn.label, cn.metadata, e.relation
                    FROM edges e
                    JOIN nodes cn ON cn.id = CASE WHEN e.source_id=? THEN e.target_id
                                                  ELSE e.source_id END
                    WHERE (e.source_id=? OR e.target_id=?) AND cn.type='Concept'
                    ORDER BY cn.label
                    """,
                    (d["id"], d["id"], d["id"]),
                ).fetchall()
                if rows:
                    concepts = [
                        {"id": r["id"], "label": r["label"], "type": r["type"],
                         "metadata": json.loads(r["metadata"] or "{}")}
                        for r in rows
                    ]
                    zf.writestr(
                        f"connections/{_flat(src)}.json",
                        json.dumps({"source": src, "concepts": concepts},
                                   indent=2, ensure_ascii=False),
                    )
        except Exception:
            log.debug("Connections export failed for group %s", group_id, exc_info=True)

        # annotations/ — highlights and notes (read from the annotations store)
        for doc_name, data in list_group_annotations(username, group_id).items():
            if not data:
                continue
            try:
                zf.writestr(f"annotations/{_flat(doc_name)}.json",
                            json.dumps(data, indent=2, ensure_ascii=False))
            except Exception:
                log.debug("Skipped annotations %s in export", doc_name, exc_info=True)

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )
