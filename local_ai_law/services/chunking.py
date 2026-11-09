"""Chunking and chunk ID generation."""

import hashlib
import re

MIN_CHUNK_CHARS = 30

_TABLE_RE = re.compile(r'\[TABLE\]\n(.*?)\n\[/TABLE\]', re.DOTALL)


def chunk_ok(text: str) -> bool:
    """True if a chunk has enough real content to keep (>= MIN_CHUNK_CHARS after stripping code fences)."""
    clean = re.sub(r'```\w*\n?|```', '', text).strip()
    return len(clean) >= MIN_CHUNK_CHARS


def _split_tables_and_text(text: str) -> list[tuple[str, bool]]:
    """Split text into alternating (content, is_table) segments."""
    parts = []
    last_end = 0
    for m in _TABLE_RE.finditer(text):
        before = text[last_end:m.start()].strip()
        if before:
            parts.append((before, False))
        parts.append((m.group(1).strip(), True))
        last_end = m.end()
    trailing = text[last_end:].strip()
    if trailing:
        parts.append((trailing, False))
    return parts if parts else [(text, False)]


def _heading_of(line: str) -> tuple[int, str] | None:
    """Return (level, heading text) if the line is a heading, else None.
    Mirrors the heading test used for semantic splitting."""
    stripped = line.strip()
    if stripped.startswith('#'):
        level = len(stripped) - len(stripped.lstrip('#'))
        text = stripped.lstrip('#').strip()
        return (min(level, 4), text) if text else None
    if re.match(r'^Art\.\s*\d+', stripped):
        return (2, stripped[:80])
    if (5 <= len(stripped) <= 80
            and stripped == stripped.upper()
            and any(c.isalpha() for c in stripped)
            and not stripped.endswith('.')):
        return (2, stripped.title())
    return None


def _semantic_sections(text: str) -> list[str]:
    """Split text on markdown headings, article numbers, or ALL-CAPS section titles."""
    lines = text.split('\n')
    sections, cur = [], []
    for line in lines:
        if _heading_of(line) is not None and cur:
            sections.append('\n'.join(cur).strip())
            cur = [line]
        else:
            cur.append(line)
    if cur:
        sections.append('\n'.join(cur).strip())
    return [s for s in sections if s]


def _word_window_chunks(text: str, size: int, overlap: int, src: str, page: int,
                        heading_path: str = "") -> list[dict]:
    """Slide a `size`-word window (with `overlap`) over text → chunk dicts carrying
    source/page/heading_path. Short text becomes a single chunk."""
    words = text.split()
    if not words:
        return []
    if len(words) <= size:
        return [{"text": " ".join(words), "source": src, "page": page,
                 "heading_path": heading_path}]
    out = []
    start = 0
    while start < len(words):
        end = min(start + size, len(words))
        out.append({"text": " ".join(words[start:end]), "source": src, "page": page,
                    "heading_path": heading_path})
        if end == len(words):
            break
        start += size - overlap
    return out


def split_chunks(raw: list[dict], size: int, overlap: int) -> list[dict]:
    """
    Split raw items into chunks.

    Strategy:
      1. If no_split=True (code cells) -> emit as-is.
      2. Separate [TABLE] blocks from prose; tables emit as whole chunks.
      3. Prose: semantic split on headings, then word-window with overlap.

    A heading stack is tracked across the whole file (raw is one document,
    in page order) so every chunk carries heading_path ("H1 > H2") for
    section-aware citations. Always a string — ChromaDB rejects None.
    """
    out = []
    stack: list[tuple[int, str]] = []  # (level, heading text)

    def _path() -> str:
        return " > ".join(t for _, t in stack)

    def _push(level: int, text: str):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, text))

    for item in raw:
        src  = item["source"]
        page = item["page"]
        text = item["text"]

        if item.get("no_split"):
            out.append({"text": text, "source": src, "page": page, "heading_path": _path()})
            continue

        segments = _split_tables_and_text(text)

        for content, is_table in segments:
            if is_table:
                # Keep tables intact; split only if very large
                lines = content.split('\n')
                if len(lines) <= 60:
                    out.append({"text": content, "source": src, "page": page,
                                "heading_path": _path()})
                else:
                    header = lines[:2]
                    chunk_lines = []
                    for line in lines[2:]:
                        chunk_lines.append(line)
                        if len(chunk_lines) >= 40:
                            out.append({"text": "\n".join(header + chunk_lines), "source": src,
                                        "page": page, "heading_path": _path()})
                            chunk_lines = []
                    if chunk_lines:
                        out.append({"text": "\n".join(header + chunk_lines), "source": src,
                                    "page": page, "heading_path": _path()})
                continue

            sections = _semantic_sections(content) if len(content) > 200 else [content]
            for section in sections:
                h = _heading_of(section.split('\n', 1)[0])
                if h:
                    _push(*h)
                out.extend(_word_window_chunks(section, size, overlap, src, page, _path()))

    return out


def chunk_id(c: dict) -> str:
    """Content-hash based ID  -  deduplicates by actual text content, not source:page."""
    key = f"{c['source']}::{c['text']}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
