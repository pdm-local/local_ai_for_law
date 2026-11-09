"""Document upload, delete, list, preview, serve  -  per-user data isolation.

Document identity is the validated **relative path** within a group (folder + name)
- see ``_safe_rel``. That rel_path is canonical everywhere: chunk metadata (``source``),
indexed status, summaries, annotations, deletion, export and preview all key on it, so
same-basename siblings in different folders never collide. The residual ``path.name``
(basename) fallbacks in delete/summary lookups exist ONLY to keep documents that were
indexed before rel_path was canonical still addressable; do not extend basename scope
to new features.
"""

import re
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from local_ai_law.config import (
    SUPPORTED_EXTS, MAX_UPLOAD_BYTES, MAX_UPLOAD_MB,
    get_group, group_doc_paths, group_upload_dir, load_settings,
    load_group_manifest, save_group_manifest,
    DEFAULT_SETTINGS, log,
)
from local_ai_law.routes.auth import get_current_username
from local_ai_law.services.audit import audit_log
from local_ai_law.services.indexer import (
    get_group_collection, group_chunk_stats, do_index_group, idx_status,
)
from local_ai_law.services.extraction import extract_docx
from local_ai_law.services.doc_summary import (
    load_doc_summaries, get_doc_summary, summarize_document,
    save_doc_summaries, summarize_all_docs,
)
from local_ai_law.services.annotations import load_annotations, add_annotation, update_annotation, delete_annotation, list_group_annotations
from local_ai_law.models import AnnotationIn, FileCreateIn, FolderCreateIn, FileContentIn

router = APIRouter(prefix="/api", tags=["documents"])

# ── Security: filename sanitization ──────────────────────────────────────────

_SAFE_FILENAME_RE = re.compile(r'[^a-zA-Z0-9._\- ()]+')


def sanitize_filename(name: str) -> str:
    """Make an uploaded filename safe: URL-decode, NFC-normalize, strip control chars and
    path separators (no traversal), allow-list characters, cap at 200 chars."""
    import unicodedata, re as _re
    from urllib.parse import unquote
    name = unquote(name)
    name = unicodedata.normalize('NFC', name)
    name = _re.sub(r'[\x00-\x1f\x7f]', '_', name)
    name = Path(name.replace("\\", "/")).name
    name = _SAFE_FILENAME_RE.sub("_", name)
    name = name.strip("_. ")
    if not name:
        name = "unnamed_file"
    return name[:200]


# ── Content-type validation ──────────────────────────────────────────────────

_VALID_MIMES = {
    ".pdf":  {"application/pdf"},
    ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
    ".pptx": {"application/vnd.openxmlformats-officedocument.presentationml.presentation"},
    ".xlsx": {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ".csv":  {"text/csv", "application/csv", "text/plain"},
    ".txt":  {"text/plain", "application/octet-stream"},
    ".md":   {"text/plain", "text/markdown", "application/octet-stream"},
    ".html": {"text/html", "application/octet-stream"},
    ".htm":  {"text/html", "application/octet-stream"},
    ".ipynb": {"application/json", "application/x-ipynb+json", "application/octet-stream"},
    ".epub": {"application/epub+zip", "application/octet-stream"},
    ".json": {"application/json", "text/plain", "application/octet-stream"},
    ".xml":  {"text/xml", "application/xml", "application/octet-stream"},
}


def _validate_content_type(ext: str, content_type: str | None) -> bool:
    """True if the uploaded MIME plausibly matches the extension (fail-open on unknown/missing)."""
    if not content_type:
        return True
    valid = _VALID_MIMES.get(ext, set())
    if not valid:
        return True
    return content_type in valid or content_type == "application/octet-stream"


# ── Document Endpoints ───────────────────────────────────────────────────────

@router.get("/groups/{group_id}/documents")
async def api_group_docs(group_id: str, username: str = Depends(get_current_username)):
    """GET — list a group's documents (with size/chunk-count/indexed flags) and its folders."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    src_counts = group_chunk_stats(group_id, username)
    base = group_upload_dir(group_id, username)
    docs = []
    for p in group_doc_paths(group_id, group, username):
        rel = p.relative_to(base).as_posix()
        folder_rel = p.parent.relative_to(base).as_posix() if p.parent != base else ""
        docs.append({
            "name":      p.name,
            "rel_path":  rel,
            "folder":    folder_rel,
            "size":      p.stat().st_size,
            "modified":  datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
            "chunks":    src_counts.get(rel, 0),
            "indexed":   rel in src_counts,
            "editable":  p.suffix.lower() in {".txt", ".md"},
            "deletable": True,
        })
    # List subdirectories so the frontend can show them
    folders = sorted(
        p.relative_to(base).as_posix()
        for p in base.rglob("*") if p.is_dir()
    )
    return {"documents": docs, "total_chunks": sum(src_counts.values()), "folders": folders}


@router.post("/groups/{group_id}/upload")
async def api_group_upload(
    group_id: str,
    username: str = Depends(get_current_username),
    file: UploadFile = File(...),
):
    """POST — upload one document into a group (validates name/size/MIME, then saves to the group dir)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")

    safe_name = sanitize_filename(file.filename or "unnamed")
    ext = Path(safe_name).suffix.lower()
    if ext not in SUPPORTED_EXTS:
        raise HTTPException(400, f"Unsupported type: {ext}")

    if not _validate_content_type(ext, file.content_type):
        raise HTTPException(400, f"Content type {file.content_type} doesn't match extension {ext}")

    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, f"File too large (max {MAX_UPLOAD_MB}MB)")

    dest = group_upload_dir(group_id, username) / safe_name
    upload_dir = group_upload_dir(group_id, username)
    if not dest.resolve().is_relative_to(upload_dir.resolve()):
        raise HTTPException(400, "Invalid filename")

    with open(dest, "wb") as fh:
        fh.write(content)

    log.info("Uploaded %s (%d bytes) to group %s", safe_name, len(content), group_id)
    audit_log(username, "upload", target=f"{group_id}/{safe_name}", details=f"{len(content)} bytes")

    _index_soon(group_id, username)
    return {"message": f"Uploaded {safe_name}"}


def _index_soon(group_id: str, username: str) -> None:
    """Start a background (LOW pool) index run for the group unless one is already going."""
    st = idx_status(group_id, username)
    if not st.is_indexing:
        from local_ai_law.services.jobs import create_job, submit_job
        job_id = create_job("index_group", username, group_id)
        submit_job(job_id, lambda ctx: do_index_group(group_id, username, False), priority="LOW")


@router.delete("/groups/{group_id}/documents/{rel_path:path}")
async def api_group_delete_doc(group_id: str, rel_path: str, username: str = Depends(get_current_username)):
    """DELETE — remove a document file and its chunks from the vector index (audit-logged)."""
    base = group_upload_dir(group_id, username)
    path = _safe_rel(base, rel_path)
    rel = path.relative_to(base).as_posix()
    if not path.exists():
        raise HTTPException(404, "File not found")
    try:
        col = get_group_collection(group_id, username)
        all_data = col.get()
        to_del = [
            id_ for id_, meta in zip(all_data["ids"], all_data["metadatas"])
            if meta.get("source") in {rel, path.name}
        ]
        if to_del:
            col.delete(ids=to_del)
    except Exception as e:
        log.warning("Delete chunks: %s", e)
    path.unlink()
    audit_log(username, "delete_doc", target=f"{group_id}/{rel}")
    return {"message": f"Deleted {path.name}"}


@router.get("/groups/{group_id}/documents/{rel_path:path}/file")
async def serve_document_file(group_id: str, rel_path: str, username: str = Depends(get_current_username)):
    """GET — stream the raw document file back to the client (traversal-guarded)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    return FileResponse(str(_resolve_file_path(group_id, rel_path, username)))


@router.get("/groups/{group_id}/documents/{rel_path:path}/preview")
async def preview_document_content(group_id: str, rel_path: str, username: str = Depends(get_current_username)):
    """GET — extracted text preview for in-browser viewing (currently .docx → pages)."""
    path = _resolve_file_path(group_id, rel_path, username)
    ext = path.suffix.lower()
    if ext == ".docx":
        pages = extract_docx(path)
        return {"type": "docx", "pages": [{"text": p["text"], "page": p["page"]} for p in pages]}
    raise HTTPException(400, f"No text-preview available for {ext}; use /file instead")


# ── Summaries ────────────────────────────────────────────────────────────────

@router.get("/groups/{group_id}/documents/{rel_path:path}/summary")
async def api_get_doc_summary(group_id: str, rel_path: str, username: str = Depends(get_current_username)):
    """GET — the cached card/summary for one document (status 'not_generated' if none)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    path = _resolve_file_path(group_id, rel_path, username)
    rel = path.relative_to(group_upload_dir(group_id, username)).as_posix()
    summary = (
        get_doc_summary(group_id, rel, username)
        or get_doc_summary(group_id, path.name, username)
        or get_doc_summary(group_id, rel_path, username)
    )
    if not summary:
        return {"summary": None, "status": "not_generated"}
    return {"summary": summary, "status": "ready"}


@router.post("/groups/{group_id}/documents/{rel_path:path}/summary/generate")
async def api_generate_doc_summary(
    group_id: str, rel_path: str,
    background: BackgroundTasks,
    username: str = Depends(get_current_username),
):
    """POST — (re)generate the card/summary for one document in the background."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    col = get_group_collection(group_id, username)
    if col.count() == 0:
        raise HTTPException(412, "No indexed content  -  run Re-index first")
    cfg = load_settings(username)
    model = cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
    path = _resolve_file_path(group_id, rel_path, username)
    rel = path.relative_to(group_upload_dir(group_id, username)).as_posix()

    def _run():
        # Identify the document by its relative path — chunk metadata is keyed by
        # the group-relative source, so path.name would miss nested files (and a
        # basename alias could attach the summary to the wrong duplicate-named doc).
        summary = summarize_document(group_id, rel, col, model)
        if summary:
            existing = load_doc_summaries(group_id, username)
            existing[rel] = summary
            save_doc_summaries(group_id, existing, username)

    background.add_task(_run)
    return {"status": "generating", "doc_name": rel}


@router.get("/groups/{group_id}/summaries")
async def api_list_doc_summaries(group_id: str, username: str = Depends(get_current_username)):
    """GET — all cached document summaries for a group."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    return {"summaries": load_doc_summaries(group_id, username)}


@router.post("/groups/{group_id}/summaries/regenerate")
async def api_regenerate_summaries(
    group_id: str,
    background: BackgroundTasks,
    username: str = Depends(get_current_username),
):
    """POST — force-regenerate summaries/cards for every document in the group (background)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    col = get_group_collection(group_id, username)
    if col.count() == 0:
        raise HTTPException(412, "No indexed content  -  run Re-index first")
    cfg = load_settings(username)
    model = cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
    paths = group_doc_paths(group_id, group, username)

    def _run():
        summarize_all_docs(group_id, username, paths, col, model=model, force=True)

    background.add_task(_run)
    return {"status": "generating", "count": len(paths)}


# ── Annotations ──────────────────────────────────────────────────────────────

@router.get("/groups/{group_id}/documents/{rel_path:path}/annotations")
async def api_get_annotations(group_id: str, rel_path: str, username: str = Depends(get_current_username)):
    """GET — all highlights/notes anchored to one document."""
    path = _resolve_file_path(group_id, rel_path, username)
    rel = path.relative_to(group_upload_dir(group_id, username)).as_posix()
    return {"annotations": load_annotations(username, group_id, rel)}


@router.post("/groups/{group_id}/documents/{rel_path:path}/annotations")
async def api_add_annotation(
    group_id: str, rel_path: str,
    body: AnnotationIn,
    username: str = Depends(get_current_username),
):
    """POST — add a highlight/note to a character span of a document."""
    path = _resolve_file_path(group_id, rel_path, username)
    rel = path.relative_to(group_upload_dir(group_id, username)).as_posix()
    ann = add_annotation(
        username, group_id, rel,
        body.start_offset, body.end_offset,
        body.selected_text, body.note, body.color,
    )
    return {"annotation": ann}


@router.put("/groups/{group_id}/annotations/{annotation_id}")
async def api_update_annotation(
    group_id: str, annotation_id: str,
    body: AnnotationIn,
    username: str = Depends(get_current_username),
):
    """PUT — edit an existing annotation's note/colour."""
    result = update_annotation(username, group_id, body.doc_name, annotation_id, body.note, body.color)
    if not result:
        raise HTTPException(404, "Annotation not found")
    return {"annotation": result}


@router.delete("/groups/{group_id}/annotations/{annotation_id}")
async def api_delete_annotation(
    group_id: str, annotation_id: str,
    doc_name: str = Query(...),
    username: str = Depends(get_current_username),
):
    """DELETE — remove one annotation from a document."""
    if not delete_annotation(username, group_id, doc_name, annotation_id):
        raise HTTPException(404, "Annotation not found")
    return {"message": "Annotation deleted"}


@router.get("/groups/{group_id}/annotations")
async def api_list_group_annotations(group_id: str, username: str = Depends(get_current_username)):
    """GET — every annotation across all documents in a group."""
    return {"annotations": list_group_annotations(username, group_id)}


# ── File manager: tree + manifest ────────────────────────────────────────────

@router.get("/groups/{group_id}/files/tree")
async def api_file_tree(group_id: str, username: str = Depends(get_current_username)):
    """Full recursive tree with per-file indexing status.

    While the group is indexing, each supported file not yet ready carries
    `progress` (0..100, from IdxStatus.file_progress; 0 = waiting its turn)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)
    manifest = load_group_manifest(group_id, username)
    disabled = set(manifest.get("disabled", []))
    src_counts = group_chunk_stats(group_id, username)
    st = idx_status(group_id, username)
    with st._lock:
        indexing = st.is_indexing
        progress = dict(st.file_progress)

    files = []
    folders = []
    for p in sorted(base.rglob("*")):
        if p.name == "_manifest.json":
            continue
        rel = p.relative_to(base).as_posix()
        if p.is_dir():
            folders.append(rel)
        elif p.is_file():
            enabled = rel not in disabled
            files.append({
                "name":      p.name,
                "rel_path":  rel,
                "folder":    p.parent.relative_to(base).as_posix() if p.parent != base else "",
                "size":      p.stat().st_size,
                "modified":  datetime.fromtimestamp(p.stat().st_mtime).isoformat(),
                "ext":       p.suffix.lower(),
                "supported": p.suffix.lower() in SUPPORTED_EXTS,
                "editable":  p.suffix.lower() in {".txt", ".md"},
                "enabled":   enabled,
                "chunks":    src_counts.get(rel, 0),
                "indexed":   rel in src_counts and enabled,
            })
            pct = progress.get(rel, 0)
            if indexing and enabled and files[-1]["supported"] and pct < 100:
                files[-1]["progress"] = pct
    return {"files": files, "folders": folders, "disabled": list(disabled), "indexing": indexing}


@router.post("/groups/{group_id}/files/manifest")
async def api_update_manifest(
    group_id: str,
    body: dict,
    username: str = Depends(get_current_username),
):
    """Set the list of rel_paths excluded from indexing."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)
    disabled = body.get("disabled", [])
    # path-traversal guard
    for rel in disabled:
        _safe_rel(base, rel)
    save_group_manifest(group_id, username, {"disabled": disabled})
    audit_log(username, "update_manifest", target=group_id,
              details=f"{len(disabled)} excluded")
    return {"message": "Manifest updated", "disabled_count": len(disabled)}


# ── File / Folder creation & editing ─────────────────────────────────────────

_TEXT_EXTS = {".txt", ".md"}


def _safe_rel(base: Path, rel: str) -> Path:
    """Resolve a relative path safely under base; raises 400 on traversal."""
    rel = rel.replace("\\", "/")
    if rel.startswith("/"):
        raise HTTPException(400, "Invalid path")
    rel = rel.strip("/")
    parts = PurePosixPath(rel).parts
    if not rel or not parts or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(400, "Invalid path")
    target = (base / Path(*parts)).resolve()
    base_resolved = base.resolve()
    if not target.is_relative_to(base_resolved):
        raise HTTPException(400, "Invalid path")
    return target


@router.post("/groups/{group_id}/files")
async def api_create_file(
    group_id: str,
    body: FileCreateIn,
    username: str = Depends(get_current_username),
):
    """POST: create a new .txt/.md document in the group (optionally in a folder), then index it
    like an upload. The Note dialog saves notes through this."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)

    safe_name = sanitize_filename(body.name)
    if not Path(safe_name).suffix:
        safe_name += ".md"
    if Path(safe_name).suffix.lower() not in _TEXT_EXTS:
        raise HTTPException(400, "Only .txt and .md files can be created")

    folder_dir = _safe_rel(base, body.folder) if body.folder.strip() else base
    folder_dir.mkdir(parents=True, exist_ok=True)

    dest = folder_dir / safe_name
    if dest.exists():
        raise HTTPException(409, f"{safe_name} already exists in that folder")

    dest.write_text(body.content, encoding="utf-8")
    rel = dest.relative_to(base).as_posix()
    audit_log(username, "create_file", target=f"{group_id}/{rel}")
    _index_soon(group_id, username)
    return {"message": f"Created {safe_name}", "rel_path": rel}


@router.post("/groups/{group_id}/folders")
async def api_create_folder(
    group_id: str,
    body: FolderCreateIn,
    username: str = Depends(get_current_username),
):
    """POST — create a new folder in the group's document tree."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)

    safe_name = sanitize_filename(body.name)
    parent_dir = _safe_rel(base, body.parent) if body.parent.strip() else base
    new_dir = parent_dir / safe_name

    if not new_dir.resolve().is_relative_to(base.resolve()):
        raise HTTPException(400, "Invalid path")
    if new_dir.exists():
        raise HTTPException(409, "Folder already exists")

    new_dir.mkdir(parents=True, exist_ok=True)
    rel = new_dir.relative_to(base).as_posix()
    audit_log(username, "create_folder", target=f"{group_id}/{rel}")
    return {"message": f"Created folder {safe_name}", "rel_path": rel}


@router.delete("/groups/{group_id}/folders/{folder_path:path}")
async def api_delete_folder(group_id: str, folder_path: str, username: str = Depends(get_current_username)):
    """DELETE — remove a folder and every document under it: deletes each file's chunks
    from the vector index, then removes the folder subtree (traversal-guarded)."""
    import shutil
    base = group_upload_dir(group_id, username)
    target = _safe_rel(base, folder_path)
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, "Folder not found")
    files = [p for p in target.rglob("*") if p.is_file()]
    rels = [p.relative_to(base).as_posix() for p in files]
    if rels:
        try:
            col = get_group_collection(group_id, username)
            all_data = col.get()
            names = set(rels) | {p.name for p in files}
            to_del = [
                id_ for id_, meta in zip(all_data["ids"], all_data["metadatas"])
                if meta.get("source") in names
            ]
            if to_del:
                col.delete(ids=to_del)
        except Exception as e:
            log.warning("Delete folder chunks: %s", e)
    shutil.rmtree(target)
    rel = target.relative_to(base).as_posix()
    audit_log(username, "delete_folder", target=f"{group_id}/{rel}", details=f"{len(rels)} files")
    return {"message": f"Deleted folder ({len(rels)} file(s))", "deleted": len(rels)}


@router.get("/groups/{group_id}/files/{rel_path:path}/content")
async def api_get_file_content(
    group_id: str,
    rel_path: str,
    username: str = Depends(get_current_username),
):
    """GET — raw text of an editable (.txt/.md) document for the in-app editor."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)
    target = _safe_rel(base, rel_path)
    if not target.exists():
        raise HTTPException(404, "File not found")
    if target.suffix.lower() not in _TEXT_EXTS:
        raise HTTPException(400, "Only .txt and .md files are editable")
    return {"content": target.read_text(encoding="utf-8", errors="replace"),
            "rel_path": target.relative_to(base).as_posix()}


@router.put("/groups/{group_id}/files/{rel_path:path}/content")
async def api_save_file_content(
    group_id: str,
    rel_path: str,
    body: FileContentIn,
    username: str = Depends(get_current_username),
):
    """PUT — overwrite an editable (.txt/.md) document's content (audit-logged)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)
    target = _safe_rel(base, rel_path)
    if not target.exists():
        raise HTTPException(404, "File not found")
    if target.suffix.lower() not in _TEXT_EXTS:
        raise HTTPException(400, "Only .txt and .md files are editable")
    target.write_text(body.content, encoding="utf-8")
    audit_log(username, "edit_file", target=f"{group_id}/{rel_path}")
    return {"message": "Saved"}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_file_path(group_id: str, filename: str, username: str) -> Path:
    """Resolve a group-relative filename to an existing file under the group dir (traversal-guarded; 404 if missing)."""
    group = get_group(group_id, username)
    if not group:
        raise HTTPException(404, "Group not found")
    base = group_upload_dir(group_id, username)
    upload_path = _safe_rel(base, filename)
    if upload_path.exists() and upload_path.is_file():
        return upload_path
    raise HTTPException(404, "File not found")
