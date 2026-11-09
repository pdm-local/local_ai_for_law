"""Backup and restore, end to end on a throwaway account.

A backup is a copy of every document a firm uploaded, so: the file is owner-only, the
knowledge DB (WAL mode) is snapshotted through SQLite rather than copied raw, a restore
brings back exactly what was backed up, it refuses while jobs run, and a restore that
fails halfway puts the old data back.

Hermetic: RAG_DATA_DIR points at a temp tree (see conftest), no Ollama/GPU needed.
"""
from __future__ import annotations

import os
import stat
import tarfile

import pytest

from local_ai_law.config import user_data_dir
from local_ai_law.services import backup, graph

U = "dora"


def _rows() -> list[str]:
    return [r[0] for r in graph._conn(U).execute("SELECT v FROM t ORDER BY v").fetchall()]


@pytest.fixture
def account(monkeypatch):
    monkeypatch.setattr(backup, "_busy_jobs", lambda username: [])
    d = user_data_dir(U)
    (d / "uploads" / "g1").mkdir(parents=True, exist_ok=True)
    (d / "uploads" / "g1" / "contract.txt").write_text("original clause")
    (d / "groups.json").write_text('[{"id": "g1", "name": "Matter"}]')
    c = graph._conn(U)
    c.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    c.execute("DELETE FROM t")
    c.execute("INSERT INTO t VALUES ('kept')")
    c.commit()
    yield d
    graph.reset_connections(U)


def test_backup_is_owner_only_and_has_no_sqlite_side_files(account):
    path = backup.create_backup(U)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with tarfile.open(path) as tar:
        names = tar.getnames()
    assert "knowledge.db" in names
    assert "uploads/g1/contract.txt" in names
    assert not [n for n in names if n.endswith(("-wal", "-shm")) or n.startswith("backups")]
    assert not list(path.parent.glob(".*.partial"))


def test_restore_round_trip(account):
    name = backup.create_backup(U).name
    # Change everything after the backup.
    (account / "uploads" / "g1" / "contract.txt").write_text("edited clause")
    (account / "uploads" / "g1" / "new.txt").write_text("added later")
    graph._conn(U).execute("INSERT INTO t VALUES ('added')")
    graph._conn(U).commit()
    assert _rows() == ["added", "kept"]

    assert backup.restore_backup(U, name) is True
    assert (account / "uploads" / "g1" / "contract.txt").read_text() == "original clause"
    assert not (account / "uploads" / "g1" / "new.txt").exists()
    # The cached connection was dropped: this reads the restored file, not the old one.
    assert _rows() == ["kept"]
    assert (account / "backups" / name).exists()
    assert not (account / ".restore_staging").exists() and not (account / ".restore_old").exists()


def test_restore_refuses_while_jobs_run(account, monkeypatch):
    name = backup.create_backup(U).name
    (account / "groups.json").write_text("[]")
    monkeypatch.setattr(backup, "_busy_jobs", lambda username: [{"id": "j1"}])
    with pytest.raises(backup.RestoreBusy):
        backup.restore_backup(U, name)
    assert (account / "groups.json").read_text() == "[]"


def test_failed_swap_puts_old_data_back(account, monkeypatch):
    name = backup.create_backup(U).name
    (account / "groups.json").write_text("[]")
    real_replace = os.replace
    moved_in = []

    def flaky_replace(src, dst):
        if ".restore_staging" in str(src):
            if moved_in:
                raise OSError("disk full")
            moved_in.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", flaky_replace)
    assert backup.restore_backup(U, name) is False
    monkeypatch.setattr(backup.os, "replace", real_replace)
    assert (account / "groups.json").read_text() == "[]"
    assert (account / "uploads" / "g1" / "contract.txt").read_text() == "original clause"
    assert _rows() == ["kept"]
    assert not (account / ".restore_staging").exists() and not (account / ".restore_old").exists()
