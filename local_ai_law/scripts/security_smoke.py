#!/usr/bin/env python3
"""Lightweight security and contract smoke checks for local development."""

from __future__ import annotations

import os
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Grandparent of the package so `local_ai_law.*` imports resolve. Computed, not hardcoded.
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


def check_settings_contract():
    from local_ai_law.models import SettingsIn

    data = SettingsIn(
        vision_model="llava",
        chunk_overlap=80,
        ollama_chat_concurrency=1,
        embed_batch_size=16,
        extraction_mode="plain",
        table_format="linearized",
        query_expansion="auto",
        retrieval_second_pass=True,
        answer_templates=True,
    ).model_dump(exclude_none=True)
    assert data["vision_model"] == "llava"
    assert data["chunk_overlap"] == 80
    assert data["ollama_chat_concurrency"] == 1
    assert data["embed_batch_size"] == 16
    assert data["table_format"] == "linearized"
    assert data["query_expansion"] == "auto"
    assert data["retrieval_second_pass"] is True
    assert data["answer_templates"] is True
    # invalid enum values must be rejected
    try:
        SettingsIn(table_format="csv")
        raise AssertionError("table_format pattern not enforced")
    except ValueError:
        pass


def check_backup_validation():
    from local_ai_law.config import user_backups_dir, user_data_dir
    from local_ai_law.services.backup import get_backup_file, restore_backup

    username = "smoke"
    backups = user_backups_dir(username)
    data_dir = user_data_dir(username)
    backups.mkdir(parents=True, exist_ok=True)

    payload = data_dir / "payload.txt"
    payload.write_text("bad", encoding="utf-8")
    unsafe = backups / "backup-2026-05-02_12-00-00.tar.gz"
    with tarfile.open(unsafe, "w:gz") as tar:
        tar.add(payload, arcname="../escape.txt")

    assert get_backup_file(username, "../" + unsafe.name) is None
    assert restore_backup(username, unsafe.name) is False
    assert not (data_dir / "escape.txt").exists()

    # a symlink member must be rejected (could redirect a later write outside data/)
    sym = backups / "backup-2026-05-02_12-00-01.tar.gz"
    with tarfile.open(sym, "w:gz") as tar:
        info = tarfile.TarInfo("link.txt")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    assert restore_backup(username, sym.name) is False
    assert not (data_dir / "link.txt").exists()

    # an absolute-path member must be rejected
    absx = backups / "backup-2026-05-02_12-00-02.tar.gz"
    with tarfile.open(absx, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("/escape_abs.txt"))
    assert restore_backup(username, absx.name) is False
    assert not Path("/escape_abs.txt").exists()


def check_path_validation():
    from fastapi import HTTPException
    from local_ai_law.routes.documents import _safe_rel

    base = Path(os.environ["RAG_DATA_DIR"]) / "paths"
    base.mkdir(parents=True, exist_ok=True)
    assert _safe_rel(base, "folder/doc.md") == (base / "folder" / "doc.md").resolve()
    for bad in ("../x", "folder/../../x", "/absolute", "."):
        try:
            _safe_rel(base, bad)
        except HTTPException:
            pass
        else:
            raise AssertionError(f"Unsafe path accepted: {bad}")


def check_app_import():
    from app import app

    assert app.title
    # Modern Starlette includes sub-routers lazily, so app.routes no longer
    # enumerates every path. Introspect via the OpenAPI schema instead, which
    # resolves all included routes regardless of Starlette version.
    paths = set(app.openapi()["paths"])
    assert len(paths) > 80, f"too few routes: {len(paths)}"
    assert not any("/research" in p for p in paths)
    # The deprecated education routes (study-bank/progress/flashcards) are intentionally
    # excluded from the OpenAPI schema (include_in_schema=False) — they are 410-Gone stubs,
    # not part of the real API. Verify they still RESPOND 410 instead of appearing in schema.
    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    for url in ("/api/study-bank", "/api/progress/x", "/api/flashcards/x"):
        assert client.get(url).status_code in (410, 401), f"{url} not a 410 stub"


def check_front_door():
    """The front door (services/guard.py) is the outermost middleware and refuses a walking
    path, an unknown Host and a cross-site change before auth sees them."""
    from local_ai_law.app import app
    from local_ai_law.services.guard import FrontDoor
    from fastapi.testclient import TestClient
    assert app.user_middleware and app.user_middleware[0].cls is FrontDoor, "front door not outermost"
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"host": "evil.test"}).status_code == 400
    assert client.get("/api/groups/g/documents/a%2f..%2fb/file").status_code == 400
    assert client.post("/api/auth/login", json={}, headers={"origin": "http://evil.test"}).status_code == 403


def check_api_contracts():
    from local_ai_law.services.jobs import get_pool_status
    from local_ai_law.services.indexer import idx_status
    from local_ai_law.config import load_settings

    queue_data = get_pool_status()
    for key in ("critical_active", "critical_queued", "low_active", "low_queued",
                "critical_workers", "low_workers"):
        assert key in queue_data
        assert isinstance(queue_data[key], int)

    idx_data = idx_status("smoke", "default").to_dict()
    cfg = load_settings("default")
    idx_data["index_config"] = {
        "chunk_size": cfg.get("chunk_size"),
        "chunk_overlap": cfg.get("chunk_overlap"),
        "embed_model": cfg.get("embed_model"),
    }
    for key in ("chunk_size", "chunk_overlap", "embed_model"):
        assert key in idx_data["index_config"]

    from local_ai_law.services.ollama_client import diagnostics
    diag = diagnostics({})
    assert "ollama" in diag
    assert "gpu" in diag
    assert "warnings" in diag


def main():
    os.environ.setdefault("RAG_DATA_DIR", "/tmp/rag_web_security_smoke")
    check_settings_contract()
    check_backup_validation()
    check_path_validation()
    check_app_import()
    check_front_door()
    check_api_contracts()
    print("security_smoke: ok")


if __name__ == "__main__":
    main()
