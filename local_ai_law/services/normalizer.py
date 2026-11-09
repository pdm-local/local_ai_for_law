"""
Document normalizer  -  raw extraction output → canonical markdown artifact.

Takes the list of dicts from services/extraction.py and produces a single
.md file under data/<username>/normalized_docs/<group_id>/<slug>.md

The artifact includes:
  - YAML-like frontmatter (source metadata, page count, section count)
  - Page-delimited sections with headings preserved
  - Tables formatted as markdown
  - Page markers: <!-- page:N -->

This normalized artifact is the input for wiki generation and graph extraction.
It is stored and re-used to avoid re-extracting on every wiki rebuild.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from local_ai_law.config import user_data_dir, log, atomic_write_text


# ── Paths ─────────────────────────────────────────────────────────────────────

def normalized_docs_dir(username: str, group_id: str) -> Path:
    d = user_data_dir(username) / "normalized_docs" / group_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalized_doc_path(username: str, group_id: str, doc_name: str) -> Path:
    slug = _slug(doc_name)
    return normalized_docs_dir(username, group_id) / f"{slug}.md"


def _slug(name: str) -> str:
    # Slug from the FULL relative path incl. folders and extension, so that
    # a/report.md, b/report.md and report.txt map to distinct artifacts instead
    # of collapsing to "report". A path-hash suffix guarantees uniqueness even
    # when slugification collides.
    s = re.sub(r"[^\w\s-]", "-", name)
    s = re.sub(r"[\s_/]+", "-", s).strip("-").lower()
    h = hashlib.sha256(name.encode()).hexdigest()[:8]
    return f"{s[:72]}-{h}" if s else h


# ── Normalization ─────────────────────────────────────────────────────────────

def normalize_doc(
    extracted: list[dict],
    doc_name: str,
    username: str,
    group_id: str,
    extra_meta: dict | None = None,
) -> Path:
    """
    Convert extraction output into a canonical markdown artifact.
    Writes the file and returns the path.

    Each item in `extracted` has keys: text, source, page, [no_split].
    """
    if not extracted:
        log.warning("normalize_doc: no content for %s", doc_name)

    pages_seen: list[int] = []
    sections: list[str] = []

    for item in extracted:
        page = item.get("page", 1)
        # Handle image_b64 pages: substitute placeholder text instead of raw marker
        if item.get("image_b64") and len((item.get("text") or "").strip()) < 80:
            text = "[Image page — vision analysis pending]"
        else:
            text = (item.get("text") or "").strip()
        if not text:
            continue

        if page not in pages_seen:
            pages_seen.append(page)
            sections.append(f"\n<!-- page:{page} -->\n")

        # Preserve existing markdown headings; promote plain ALL-CAPS lines
        lines = text.split("\n")
        cleaned: list[str] = []
        for line in lines:
            stripped = line.strip()
            # Already a heading
            if stripped.startswith("#"):
                cleaned.append(stripped)
                continue
            # Detect implied section heading: short ALL-CAPS line
            if (
                stripped.isupper()
                and 3 <= len(stripped) <= 80
                and not stripped.startswith("|")
            ):
                cleaned.append(f"\n## {stripped.title()}\n")
                continue
            cleaned.append(line)
        sections.append("\n".join(cleaned))

    body = "\n\n".join(sections).strip()

    # Build frontmatter
    meta = {
        "source": doc_name,
        "page_count": len(pages_seen),
        "section_count": body.count("\n##"),
        "normalized_at": datetime.now(timezone.utc).isoformat(),
        "group_id": group_id,
    }
    if extra_meta:
        meta.update(extra_meta)

    fm_lines = ["---"]
    for k, v in meta.items():
        fm_lines.append(f"{k}: {json.dumps(v)}")
    fm_lines.append("---")
    frontmatter = "\n".join(fm_lines)

    content = f"{frontmatter}\n\n# {Path(doc_name).stem}\n\n{body}\n"

    out_path = normalized_doc_path(username, group_id, doc_name)
    atomic_write_text(out_path, content, encoding="utf-8")
    log.debug("Normalized %s → %s (%d chars)", doc_name, out_path.name, len(content))
    return out_path


def load_normalized(username: str, group_id: str, doc_name: str) -> str | None:
    """Load the normalized markdown for a document. Returns None if not yet normalized."""
    path = normalized_doc_path(username, group_id, doc_name)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def parse_pages(normalized_text: str) -> dict[int, str]:
    """Split normalized markdown into per-page text segments."""
    pages: dict[int, str] = {}
    current_page = 1
    current_lines: list[str] = []

    for line in normalized_text.splitlines():
        m = re.match(r"<!--\s*page:(\d+)\s*-->", line)
        if m:
            if current_lines:
                pages[current_page] = "\n".join(current_lines).strip()
            current_page = int(m.group(1))
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        pages[current_page] = "\n".join(current_lines).strip()

    return pages


def extract_sections(text: str) -> list[dict]:
    """
    Parse markdown sections from normalized text.
    Returns list of {"heading": str, "level": int, "body": str}.
    """
    sections = []
    current_heading = "(introduction)"
    current_level = 0
    current_body: list[str] = []

    for line in text.splitlines():
        m = re.match(r"^(#{1,4})\s+(.+)", line)
        if m:
            if current_body:
                sections.append({
                    "heading": current_heading,
                    "level": current_level,
                    "body": "\n".join(current_body).strip(),
                })
                current_body = []
            current_level = len(m.group(1))
            current_heading = m.group(2).strip()
        else:
            current_body.append(line)

    if current_body:
        sections.append({
            "heading": current_heading,
            "level": current_level,
            "body": "\n".join(current_body).strip(),
        })

    return [s for s in sections if s["body"]]
