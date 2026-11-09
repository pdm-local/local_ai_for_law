"""
Background job orchestration.

Jobs are stored in data/jobs.db (global, visible to admin).
Each job has a type, user, status, progress, and a cancellation flag.

Job types:
  normalize   - extract + normalize one document to markdown artifact
  wiki_build  - generate wiki pages for one document
  graph_build - extract graph relations for one document
  rebuild     - full pipeline for a group (normalize + wiki + graph)

Design:
  - Jobs run in daemon threads; UI polls GET /api/jobs/<id>.
  - A job can be cancelled: the worker checks _cancel_flags periodically.
  - Large rebuilds process docs one at a time so the main app stays responsive.
  - On restart, jobs in "running" state are reset to "interrupted" automatically.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable

from local_ai_law.config import CRITICAL_WORKERS, DATA_DIR, LOW_WORKERS, log

# ── Thread pools: CRITICAL (user-facing) vs LOW (background generation) ────────
_critical_pool = ThreadPoolExecutor(max_workers=CRITICAL_WORKERS, thread_name_prefix="job-critical")
_low_pool      = ThreadPoolExecutor(max_workers=LOW_WORKERS, thread_name_prefix="job-low")
_pool_lock = threading.Lock()
_active_by_priority = {"CRITICAL": 0, "LOW": 0}

# ── DB path ───────────────────────────────────────────────────────────────────

JOBS_DB = DATA_DIR / "jobs.db"

_local = threading.local()


def _conn() -> sqlite3.Connection:
    # Thread-local connections are never explicitly closed; the OS reclaims
    # file descriptors when the process exits. This is intentional for a
    # long-lived server process where threads outlive individual requests.
    c = getattr(_local, "jobs_conn", None)
    if c is None:
        JOBS_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(JOBS_DB), check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")
        _init_schema(c)
        _local.jobs_conn = c
    return c


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    username      TEXT NOT NULL,
    group_id      TEXT,
    doc_name      TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    priority      TEXT NOT NULL DEFAULT 'LOW',
    progress_pct  INTEGER NOT NULL DEFAULT 0,
    total_items   INTEGER NOT NULL DEFAULT 0,
    done_items    INTEGER NOT NULL DEFAULT 0,
    message       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    result        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_user   ON jobs(username);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_group  ON jobs(username, group_id);
"""


def _init_schema(c: sqlite3.Connection):
    c.executescript(_SCHEMA)
    # Add priority column to existing installs
    try:
        c.execute("ALTER TABLE jobs ADD COLUMN priority TEXT NOT NULL DEFAULT 'LOW'")
        c.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    # Reset interrupted jobs from previous runs
    c.execute(
        "UPDATE jobs SET status='interrupted', message='Server restarted' "
        "WHERE status='running'"
    )
    c.commit()


# ── Cancel flags (in-memory, per job_id) ─────────────────────────────────────

_cancel_flags: dict[str, bool] = {}
_cancel_lock = threading.Lock()


def request_cancel(job_id: str):
    with _cancel_lock:
        _cancel_flags[job_id] = True


def is_cancelled(job_id: str) -> bool:
    with _cancel_lock:
        return _cancel_flags.get(job_id, False)

def cancel_jobs_for_group(group_id: str):
    """Cancel all pending and running jobs for a group."""
    jobs = list_jobs(group_id=group_id)
    for j in jobs:
        if j["status"] in ("pending", "running"):
            request_cancel(j["id"])



# ── CRUD ─────────────────────────────────────────────────────────────────────

def create_job(job_type: str, username: str, group_id: str = "",
               doc_name: str = "", priority: str = "LOW") -> str:
    """Create a new pending job. Returns job_id."""
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).isoformat()
    _conn().execute(
        """INSERT INTO jobs(id, type, username, group_id, doc_name, status, priority,
                            created_at, updated_at)
           VALUES(?,?,?,?,?,'pending',?,?,?)""",
        (job_id, job_type, username, group_id or "", doc_name or "",
         priority.upper(), now, now),
    )
    _conn().commit()
    log.info("Job created: %s type=%s priority=%s user=%s group=%s doc=%s",
             job_id, job_type, priority, username, group_id, doc_name)
    return job_id


def get_pool_status() -> dict:
    """Return active/queued counts for both pools."""
    with _pool_lock:
        critical_active = _active_by_priority["CRITICAL"]
        low_active = _active_by_priority["LOW"]
    return {
        "critical_active": critical_active,
        "critical_queued": _critical_pool._work_queue.qsize(),
        "critical_workers": CRITICAL_WORKERS,
        "low_active": low_active,
        "low_queued": _low_pool._work_queue.qsize(),
        "low_workers": LOW_WORKERS,
    }


def get_job(job_id: str) -> dict | None:
    row = _conn().execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["result"] = json.loads(d.get("result") or "{}")
    return d


def list_jobs(username: str = "", group_id: str = "",
              status: str = "", limit: int = 50) -> list[dict]:
    q = "SELECT * FROM jobs WHERE 1=1"
    params: list = []
    if username:
        q += " AND username=?"
        params.append(username)
    if group_id:
        q += " AND group_id=?"
        params.append(group_id)
    if status:
        q += " AND status=?"
        params.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = _conn().execute(q, params).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["result"] = json.loads(d.get("result") or "{}")
        out.append(d)
    return out


def update_job(job_id: str, status: str = "", message: str = "",
               progress_pct: int = -1, done_items: int = -1,
               total_items: int = -1, result: dict | None = None):
    now = datetime.now(timezone.utc).isoformat()
    c = _conn()
    parts, params = ["updated_at=?"], [now]

    if status:
        parts.append("status=?")
        params.append(status)
    if message:
        parts.append("message=?")
        params.append(message)
    if progress_pct >= 0:
        parts.append("progress_pct=?")
        params.append(progress_pct)
    if done_items >= 0:
        parts.append("done_items=?")
        params.append(done_items)
    if total_items >= 0:
        parts.append("total_items=?")
        params.append(total_items)
    if result is not None:
        parts.append("result=?")
        params.append(json.dumps(result))

    params.append(job_id)
    c.execute(f"UPDATE jobs SET {','.join(parts)} WHERE id=?", params)
    c.commit()


# ── Job runner decorator / helper ─────────────────────────────────────────────

class JobContext:
    """Passed into worker functions so they can report progress and check cancellation."""

    def __init__(self, job_id: str, total: int = 0):
        self.job_id = job_id
        self.total = total
        self.done = 0

    def progress(self, message: str = "", step: int = 1):
        self.done += step
        pct = int(100 * self.done / max(self.total, 1))
        update_job(
            self.job_id,
            message=message,
            progress_pct=min(pct, 99),
            done_items=self.done,
            total_items=self.total,
        )

    def check_cancel(self) -> bool:
        if is_cancelled(self.job_id):
            update_job(self.job_id, status="cancelled", message="Cancelled by user")
            return True
        return False


def submit_job(
    job_id: str,
    worker: Callable[[JobContext], dict],
    total_items: int = 0,
    priority: str = "LOW",
):
    """Submit a job to the appropriate thread pool based on priority."""
    priority = priority.upper()

    def _run():
        with _pool_lock:
            _active_by_priority[priority] = _active_by_priority.get(priority, 0) + 1
        ctx = JobContext(job_id, total_items)
        update_job(job_id, status="running", message="Starting...", total_items=total_items)
        try:
            result = worker(ctx)
            # A worker that returns after honoring a cancellation must not be
            # recorded as "done" — that would erase the cancelled status.
            if is_cancelled(job_id):
                update_job(job_id, status="cancelled", message="Cancelled by user")
                log.info("Job %s cancelled", job_id)
            else:
                update_job(job_id, status="done", progress_pct=100,
                           message="Completed", result=result or {})
                log.info("Job %s completed", job_id)
        except Exception as e:
            log.error("Job %s failed: %s", job_id, e, exc_info=True)
            update_job(job_id, status="failed", message=str(e)[:500])
        finally:
            with _pool_lock:
                _active_by_priority[priority] = max(0, _active_by_priority.get(priority, 0) - 1)
            with _cancel_lock:
                _cancel_flags.pop(job_id, None)

    pool = _critical_pool if priority == "CRITICAL" else _low_pool
    pool.submit(_run)


def run_job_async(
    job_id: str,
    worker: Callable[[JobContext], dict],
    total_items: int = 0,
    priority: str = "LOW",
) -> None:
    """Backwards-compatible wrapper — routes to submit_job via thread pools."""
    submit_job(job_id, worker, total_items, priority)
