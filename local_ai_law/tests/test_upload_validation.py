"""Upload path/filename hardening — an uploaded filename or a document rel-path must
never escape the user's group upload dir. Pure path logic; no GPU/Ollama.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from local_ai_law.routes.documents import sanitize_filename, _safe_rel
from local_ai_law.config import group_upload_dir

A = "alice"


# ── filename sanitization ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,banned", [
    ("../../etc/passwd", "/"),
    ("..\\..\\windows\\system32", "\\"),
    ("a/b/c.pdf", "/"),
    ("%2e%2e%2fevil.txt", "/"),   # url-encoded ../
])
def test_sanitize_filename_strips_separators_and_traversal(raw, banned):
    out = sanitize_filename(raw)
    assert banned not in out
    assert ".." not in out
    assert out and out not in (".", "..")


def test_sanitize_filename_strips_control_chars():
    out = sanitize_filename("re\x00port\x1f.pdf")
    assert "\x00" not in out and "\x1f" not in out


# ── document rel-path containment ──────────────────────────────────────────────

@pytest.mark.parametrize("attack", [
    "../bob/secret.pdf",
    "../../bob/uploads/x.pdf",
    "/etc/passwd",
    "foo/../../bar.pdf",
    "..",
    "",
])
def test_safe_rel_rejects_traversal(attack):
    base = group_upload_dir("matter1", A)
    with pytest.raises(HTTPException):
        _safe_rel(base, attack)


def test_safe_rel_allows_legit_nested_path():
    base = group_upload_dir("matter1", A)
    p = _safe_rel(base, "correspondence/letter-001.pdf")
    assert p.resolve().is_relative_to(base.resolve())
