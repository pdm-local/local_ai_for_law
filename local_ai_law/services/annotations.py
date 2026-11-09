"""
Highlights & annotations  -  per-user, per-document text annotations.
Stored as JSON files scoped by user and group.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from local_ai_law.config import user_annotations_dir, atomic_write_json


def _rel_key(doc_name: str) -> str:
    """Normalised group-relative document path (forward slashes, no leading slash).
    This is the identity an annotation file is keyed by — the FULL path, so
    folder_a/report.md and folder_b/report.md never collapse together."""
    return doc_name.replace("\\", "/").strip("/")


def _annotations_file(username: str, group_id: str, doc_name: str) -> Path:
    """Path to a document's annotation file. The filename is a hash of the full
    relative path (not the basename) so same-named docs in different folders get
    distinct files; the human-readable rel path is preserved inside the payload."""
    d = user_annotations_dir(username) / group_id
    d.mkdir(exist_ok=True)
    rel = _rel_key(doc_name)
    stem = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]
    return d / f"{stem}.json"


def load_annotations(username: str, group_id: str, doc_name: str) -> list[dict]:
    """Return a document's annotations list (empty if none/unreadable)."""
    f = _annotations_file(username, group_id, doc_name)
    if f.exists():
        try:
            raw = json.loads(f.read_text())
        except Exception:
            return []
        # Current format wraps the list in {"doc": rel, "annotations": [...]};
        # a bare list is a legacy (basename-keyed) file — read it as-is.
        if isinstance(raw, dict):
            return raw.get("annotations", [])
        if isinstance(raw, list):
            return raw
    return []


def save_annotations(username: str, group_id: str, doc_name: str, annotations: list[dict]):
    """Atomically write a document's annotations (wrapped with its rel-path key)."""
    f = _annotations_file(username, group_id, doc_name)
    atomic_write_json(f, {"doc": _rel_key(doc_name), "annotations": annotations})


def add_annotation(
    username: str,
    group_id: str,
    doc_name: str,
    start_offset: int,
    end_offset: int,
    selected_text: str = "",
    note: str = "",
    color: str = "yellow",
) -> dict:
    """Add a highlight/note over a character span of a document; returns the new annotation."""
    annotations = load_annotations(username, group_id, doc_name)
    ann = {
        "id": str(uuid.uuid4())[:8],
        "start_offset": start_offset,
        "end_offset": end_offset,
        "selected_text": selected_text,
        "note": note,
        "color": color,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    annotations.append(ann)
    save_annotations(username, group_id, doc_name, annotations)
    return ann


def update_annotation(
    username: str,
    group_id: str,
    doc_name: str,
    annotation_id: str,
    note: str | None = None,
    color: str | None = None,
) -> dict | None:
    """Update one annotation's note/color; returns it, or None if not found."""
    annotations = load_annotations(username, group_id, doc_name)
    for a in annotations:
        if a["id"] == annotation_id:
            if note is not None:
                a["note"] = note
            if color is not None:
                a["color"] = color
            save_annotations(username, group_id, doc_name, annotations)
            return a
    return None


def delete_annotation(
    username: str,
    group_id: str,
    doc_name: str,
    annotation_id: str,
) -> bool:
    annotations = load_annotations(username, group_id, doc_name)
    before = len(annotations)
    annotations = [a for a in annotations if a["id"] != annotation_id]
    if len(annotations) < before:
        save_annotations(username, group_id, doc_name, annotations)
        return True
    return False


def list_group_annotations(username: str, group_id: str) -> dict[str, list[dict]]:
    """Get all annotations for a group, keyed by the document's full relative path.
    Reads the rel path from each file's payload; legacy bare-list files (keyed by
    basename) fall back to the filename stem."""
    d = user_annotations_dir(username) / group_id
    result = {}
    if d.exists():
        for f in d.glob("*.json"):
            try:
                raw = json.loads(f.read_text())
            except Exception:
                continue
            if isinstance(raw, dict):
                doc = raw.get("doc") or f.stem
                anns = raw.get("annotations", [])
            elif isinstance(raw, list):
                doc, anns = f.stem, raw   # legacy basename-keyed file
            else:
                continue
            if anns:
                result[doc] = anns
    return result
