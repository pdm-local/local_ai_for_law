"""
Backup & restore  -  per-user tarball snapshots of all data.
Supports manual and scheduled automatic backups.

- A backup holds a firm's documents: the tarball and its folder are owner-only (0600/0700).
- SQLite databases are copied with the SQLite backup API, never as raw files: the
  knowledge DB runs in WAL mode, and a raw copy of a live db + -wal pair can be torn.
- Restore extracts into a staging dir, moves the current data aside, swaps the staged
  data in, and only then deletes the old copy; any failure puts the old data back.
- Restore refuses while the user has a running or queued job, and drops the open
  SQLite and Chroma handles so nothing keeps writing to the replaced files.
"""

import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import re
from datetime import datetime
from pathlib import Path

from local_ai_law.config import (
    user_data_dir, user_backups_dir, user_db_path,
    BACKUP_INTERVAL_HOURS, BACKUP_KEEP_COUNT, DATA_DIR, log,
)

_scheduler_started = False
_scheduler_lock = threading.Lock()
_BACKUP_NAME_RE = re.compile(r"^backup-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.tar\.gz$")
# Never part of a backup: the backups themselves, restore scratch dirs, SQLite side files
# (the snapshot of the .db already holds their committed pages).
_SKIP_NAMES = {"backups", ".restore_staging", ".restore_old"}
_SKIP_SUFFIXES = ("-wal", "-shm", "-journal")


class RestoreBusy(Exception):
    """The user has running or queued jobs; restoring now would race them."""


def _backup_path(username: str, filename: str) -> Path | None:
    """Resolve a backup filename under the user's backup directory."""
    clean_name = Path(filename).name
    if filename != clean_name or not _BACKUP_NAME_RE.match(clean_name):
        return None
    backups = user_backups_dir(username).resolve()
    path = (backups / clean_name).resolve()
    if not path.is_relative_to(backups):
        return None
    return path


def _safe_tar_members(tar: tarfile.TarFile, data_dir: Path):
    base = data_dir.resolve()
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or Path(name).is_absolute():
            raise ValueError(f"Unsafe absolute path in backup: {name}")
        target = (base / name).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"Unsafe path in backup: {name}")
        if member.issym() or member.islnk() or member.isdev():
            raise ValueError(f"Unsafe member type in backup: {name}")
        yield member


def _owner_only_dir(path: Path) -> Path:
    try:
        os.chmod(path, 0o700)
    except OSError:
        log.debug("Could not chmod %s to 0700", path, exc_info=True)
    return path


def _is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _add_tree(tar: tarfile.TarFile, src: Path, arcname: str, tmp: Path) -> None:
    """Add src to tar under arcname; a SQLite file goes in as an online-backup snapshot."""
    if src.is_symlink():
        return  # restore refuses links, so never write one
    if src.is_dir():
        tar.add(src, arcname=arcname, recursive=False)
        for child in sorted(src.iterdir()):
            if not child.name.endswith(_SKIP_SUFFIXES):
                _add_tree(tar, child, f"{arcname}/{child.name}", tmp)
        return
    if _is_sqlite(src):
        snap = tmp / "snapshot.db"
        snap.unlink(missing_ok=True)
        s_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
        d_conn = sqlite3.connect(snap)
        try:
            s_conn.backup(d_conn)
        finally:
            d_conn.close()
            s_conn.close()
        tar.add(snap, arcname=arcname)
        snap.unlink(missing_ok=True)
        return
    tar.add(src, arcname=arcname)


def create_backup(username: str) -> Path:
    """Create an owner-only tarball backup of user data. Returns the backup file path."""
    data_dir = user_data_dir(username)
    backups = _owner_only_dir(user_backups_dir(username))
    ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    filename = f"backup-{ts}.tar.gz"
    backup_path = backups / filename
    partial = backups / f".{filename}.partial"

    # 0600 from the first byte: a backup is a copy of every document.
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w:gz") as tar, \
                tempfile.TemporaryDirectory(dir=backups) as tmp:
            for item in sorted(data_dir.iterdir()):
                if item.name in _SKIP_NAMES or item.name.endswith(_SKIP_SUFFIXES):
                    continue
                _add_tree(tar, item, item.name, Path(tmp))
        os.replace(partial, backup_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    log.info("Backup created for %s: %s (%.1f MB)",
             username, filename, backup_path.stat().st_size / 1024 / 1024)
    _prune_old_backups(username)
    return backup_path


def list_backups(username: str) -> list[dict]:
    backups = user_backups_dir(username)
    result = []
    for f in sorted(backups.glob("backup-*.tar.gz"), reverse=True):
        result.append({
            "filename": f.name,
            "size": f.stat().st_size,
            "created": datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        })
    return result


def get_backup_file(username: str, filename: str) -> Path | None:
    """Return a validated backup file path, or None."""
    path = _backup_path(username, filename)
    if not path or not path.exists() or not path.is_file():
        return None
    return path


def _busy_jobs(username: str) -> list[dict]:
    try:
        from local_ai_law.services.jobs import list_jobs
        return [j for st in ("running", "pending") for j in list_jobs(username=username, status=st)]
    except Exception:
        log.warning("Could not read the job list before restore for %s", username, exc_info=True)
        return []


def _release_handles(username: str) -> None:
    """Drop this user's cached SQLite connections and Chroma system before files move.

    graph.reset_connections closes this thread's knowledge-DB connection and makes every
    other thread reopen on next use. Chroma caches one system per persist directory; it
    is stopped and dropped so the next client opens the restored index."""
    try:
        from local_ai_law.services import graph
        graph.reset_connections(username)
    except Exception:
        log.warning("Could not reset knowledge DB connections for %s", username, exc_info=True)
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        key = str(user_db_path(username))
        system = SharedSystemClient._identifier_to_system.pop(key, None)
        SharedSystemClient._identifier_to_refcount.pop(key, None)
        if system is not None:
            system.stop()
    except Exception:
        log.warning("Could not release the Chroma client for %s", username, exc_info=True)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def restore_backup(username: str, filename: str) -> bool:
    """Restore a backup over the user's data directory. False if invalid or it failed.

    Raises RestoreBusy if the user has running or queued jobs."""
    backup_file = _backup_path(username, filename)
    if not backup_file or not backup_file.exists() or not backup_file.is_file():
        return False

    if not tarfile.is_tarfile(backup_file):
        return False

    busy = _busy_jobs(username)
    if busy:
        raise RestoreBusy(f"{len(busy)} job(s) still running or queued")

    data_dir = user_data_dir(username)
    staging = data_dir / ".restore_staging"
    old = data_dir / ".restore_old"

    # Extract to a staging dir FIRST and only swap in on success, so a failed or
    # corrupt extraction can never leave the account in a half-wiped state.
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    _owner_only_dir(staging)
    try:
        with tarfile.open(backup_file, "r:gz") as tar:
            tar.extractall(path=staging, members=_safe_tar_members(tar, staging), filter="data")
    except Exception as e:
        log.warning("Restore extraction failed (current data left intact) for %s/%s: %s",
                    username, filename, e)
        shutil.rmtree(staging, ignore_errors=True)
        return False

    _release_handles(username)

    # Move the current data aside (same filesystem: renames, not copies), then the staged
    # data in. Any failure moves everything back, so the account is never half-restored.
    old.mkdir()
    moved_out: list[str] = []
    moved_in: list[str] = []
    try:
        for item in list(data_dir.iterdir()):
            if item.name not in _SKIP_NAMES:
                os.replace(item, old / item.name)
                moved_out.append(item.name)
        for item in list(staging.iterdir()):
            if item.name not in _SKIP_NAMES:
                os.replace(item, data_dir / item.name)
                moved_in.append(item.name)
    except Exception as e:
        log.error("Restore swap failed for %s/%s, putting the old data back: %s",
                  username, filename, e)
        for name in moved_in:
            _remove(data_dir / name)
        for name in moved_out:
            os.replace(old / name, data_dir / name)
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        _release_handles(username)
        return False

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    _release_handles(username)
    log.info("Backup restored for %s from %s", username, filename)
    return True


def delete_backup(username: str, filename: str) -> bool:
    backup_file = _backup_path(username, filename)
    if backup_file and backup_file.exists() and backup_file.is_file():
        backup_file.unlink()
        return True
    return False


def _prune_old_backups(username: str):
    backups = sorted(
        user_backups_dir(username).glob("backup-*.tar.gz"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for old in backups[BACKUP_KEEP_COUNT:]:
        try:
            old.unlink()
            log.info("Pruned old backup: %s", old.name)
        except Exception:
            log.debug("Could not prune old backup %s", old.name, exc_info=True)


def _run_scheduled_backups():
    """Background loop that creates backups for all users."""
    while True:
        time.sleep(BACKUP_INTERVAL_HOURS * 3600)
        try:
            if not DATA_DIR.exists():
                continue
            for user_dir in DATA_DIR.iterdir():
                if user_dir.is_dir() and not user_dir.name.startswith("."):
                    try:
                        create_backup(user_dir.name)
                    except Exception as e:
                        log.warning("Scheduled backup failed for %s: %s", user_dir.name, e)
        except Exception as e:
            log.warning("Scheduled backup cycle error: %s", e)


def start_backup_scheduler():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        _scheduler_started = True
    if BACKUP_INTERVAL_HOURS <= 0:
        log.info("Auto-backup disabled (interval=0)")
        return
    t = threading.Thread(target=_run_scheduled_backups, daemon=True)
    t.start()
    log.info("Auto-backup scheduler started (every %dh, keep %d)",
             BACKUP_INTERVAL_HOURS, BACKUP_KEEP_COUNT)
