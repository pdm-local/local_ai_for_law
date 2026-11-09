"""Chat history / session endpoints  -  per-user data isolation."""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from local_ai_law.config import user_history_dir, log
from local_ai_law.routes.auth import get_current_username
from local_ai_law.services import citations
from local_ai_law.services.indexer import get_group_collection

router = APIRouter(prefix="/api", tags=["sessions"])


@router.get("/sessions")
async def api_sessions(
    group_id: Optional[str] = None,
    username: str = Depends(get_current_username),
):
    """GET — list the user's saved chat sessions (newest first), optionally filtered
    to one group. Each entry carries a preview (first/last question) and turn count."""
    hist_dir = user_history_dir(username)
    if not hist_dir.exists():
        return {"sessions": []}
    sessions = []
    for f in sorted(hist_dir.glob("*.json"), reverse=True):
        try:
            s = json.loads(f.read_text())
            if group_id and s.get("group_id", "default") != group_id:
                continue
            turns = s.get("turns", [])
            sessions.append({
                "id":             s["session_id"],
                "started":        s["started"],
                "model":          s.get("model", "?"),
                "group_id":       s.get("group_id", "default"),
                "group_name":     s.get("group_name", ""),
                "turns":          len(turns),
                "first_question": turns[0]["user"][:100] if turns else "",
                "last_question":  turns[-1]["user"][:100] if turns else "",
                "last_active":    turns[-1].get("timestamp", s["started"]) if turns else s["started"],
                "resumable":      True,
            })
        except Exception:
            log.debug("Skipping unreadable session file %s", f, exc_info=True)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def api_get_session(session_id: str, username: str = Depends(get_current_username)):
    """GET — the full saved session (all turns) for one session id, or 404."""
    hist_dir = user_history_dir(username)
    for f in hist_dir.glob("*.json"):
        try:
            s = json.loads(f.read_text())
            if s["session_id"] == session_id:
                return s
        except Exception:
            log.debug("Skipping unreadable session file %s", f, exc_info=True)
    raise HTTPException(404, "Session not found")


@router.get("/sessions/{session_id}/turns/{n}/passages/{k}")
async def api_cited_passage(session_id: str, n: int, k: int, username: str = Depends(get_current_username)):
    """GET — passage k of answer n, word for word as the answer was given it, and whether
    its file still says it (`state`: kept, same, changed, gone, wiki, old). A lookup by
    hash in this user's own index: never a new retrieval, never a model call."""
    session = await api_get_session(session_id, username)
    try:
        return citations.view(session, n, k, username, get_group_collection)
    except LookupError as e:
        raise HTTPException(404, str(e)) from None


@router.delete("/sessions/{session_id}")
async def api_delete_session(session_id: str, username: str = Depends(get_current_username)):
    """DELETE — remove one saved session by id, or 404 if it doesn't exist."""
    hist_dir = user_history_dir(username)
    for f in hist_dir.glob("*.json"):
        try:
            s = json.loads(f.read_text())
            if s["session_id"] == session_id:
                f.unlink()
                return {"message": "Deleted"}
        except Exception:
            log.debug("Skipping unreadable session file %s", f, exc_info=True)
    raise HTTPException(404, "Session not found")
