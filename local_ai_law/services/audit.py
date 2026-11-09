"""
Audit logging  -  structured JSON-lines audit trail.
Every significant user action is recorded with timestamp, user, action, and details.
"""

import json
import threading
from datetime import datetime, timezone

from local_ai_law.config import AUDIT_LOG, log

_lock = threading.Lock()
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB
_KEEP_ROTATED = 5


def _rotate_if_needed():
    if not AUDIT_LOG.exists():
        return
    try:
        if AUDIT_LOG.stat().st_size < _MAX_FILE_BYTES:
            return
        for i in range(_KEEP_ROTATED - 1, 0, -1):
            old = AUDIT_LOG.with_suffix(f".{i}")
            new = AUDIT_LOG.with_suffix(f".{i + 1}")
            if old.exists():
                old.rename(new)
        AUDIT_LOG.rename(AUDIT_LOG.with_suffix(".1"))
    except Exception as e:
        log.warning("Audit log rotation failed: %s", e)


def audit_log(
    user: str,
    action: str,
    target: str = "",
    details: str = "",
    ip: str = "",
):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": user,
        "action": action,
        "target": target,
        "details": details,
        "ip": ip,
    }
    with _lock:
        _rotate_if_needed()
        # Deliberately append-mode (not an atomic write-then-rename): this is an
        # append-only audit trail, and atomic replace would clobber prior lines.
        # Serialized by _lock; a torn final line is preferable to lost history.
        with open(AUDIT_LOG, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_audit_log(
    limit: int = 100,
    offset: int = 0,
    user_filter: str = "",
    action_filter: str = "",
) -> list[dict]:
    """Read audit entries (most recent first)."""
    if not AUDIT_LOG.exists():
        return []
    entries = []
    try:
        for line in AUDIT_LOG.read_text().strip().splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if user_filter and e.get("user") != user_filter:
                    continue
                if action_filter and e.get("action") != action_filter:
                    continue
                entries.append(e)
            except json.JSONDecodeError:
                continue
    except Exception:
        log.debug("Audit-log read failed; returning entries parsed so far", exc_info=True)
    entries.reverse()
    return entries[offset:offset + limit]
