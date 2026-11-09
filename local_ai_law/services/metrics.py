"""
System metrics collection for the health monitoring dashboard.
"""

import time
from datetime import datetime, timezone

import psutil

from local_ai_law.config import DATA_DIR, log
from local_ai_law.services.auth import USERS_FILE

_start_time = time.time()
_request_count = 0
_ws_connections = 0


def inc_request_count():
    """Increment the total HTTP request counter (called from the middleware)."""
    global _request_count
    _request_count += 1


def ws_connect():
    """Record a new active WebSocket connection."""
    global _ws_connections
    _ws_connections += 1


def ws_disconnect():
    """Record a WebSocket disconnect (floored at zero)."""
    global _ws_connections
    _ws_connections = max(0, _ws_connections - 1)


def get_metrics() -> dict:
    """Snapshot system + app metrics: uptime, request/WS counts, CPU/RAM/disk,
    Ollama reachability, and user count — for the admin health dashboard."""
    proc = psutil.Process()
    mem = proc.memory_info()

    disk_usage = {}
    if DATA_DIR.exists():
        for user_dir in DATA_DIR.iterdir():
            if user_dir.is_dir() and not user_dir.name.startswith("."):
                total = sum(
                    f.stat().st_size
                    for f in user_dir.rglob("*") if f.is_file()
                )
                disk_usage[user_dir.name] = total

    try:
        from local_ai_law.services.ollama_client import list_models
        models = list_models()
        ollama_ok = True
    except Exception:
        models = []
        ollama_ok = False

    user_count = 0
    # Use the configured store (RAG_USERS_FILE) — under the appliance/compose the users
    # file lives on the data volume, not next to the code, so a hardcoded path reports 0.
    if USERS_FILE.exists():
        try:
            import json
            user_count = len(json.loads(USERS_FILE.read_text()).get("users", []))
        except Exception:
            log.debug("Could not read users.json for metrics; reporting 0 users", exc_info=True)

    return {
        "uptime_seconds": int(time.time() - _start_time),
        "memory_rss_mb": round(mem.rss / 1024 / 1024, 1),
        "memory_vms_mb": round(mem.vms / 1024 / 1024, 1),
        "cpu_percent": proc.cpu_percent(interval=0.1),
        "request_count": _request_count,
        "active_ws_connections": _ws_connections,
        "disk_usage_by_user": disk_usage,
        "total_disk_mb": round(sum(disk_usage.values()) / 1024 / 1024, 1),
        "ollama_ok": ollama_ok,
        "ollama_models": models,
        "user_count": user_count,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
