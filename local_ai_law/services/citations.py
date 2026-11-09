"""The evidence behind an answer: the passages it stood on, kept with the turn.

Ported from pdm_chat's citations (2026-10-07). A chat turn used to keep only the name,
page and score of each source, so after a reindex or an edit nobody could show what an
answer had actually been given. Now each passage is copied word for word into the saved
turn, with its rel-path, page, a SHA-256 of the full text and the file version the index
held (`file_hash`, the indexer's skip-hash). Opening a citation later reads that copy and
says plainly whether the file still says it: a lookup by hash in the group's own index,
never a new retrieval and never a model call.

The copy is firm data, kept where the chat already is: the user's own `chat_history`
folder, in the backups with it. A size cap per passage and per turn stops a conversation
becoming a second copy of the matter.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from local_ai_law.config import get_group, group_upload_dir, log

#: Characters of one passage kept with the answer, and of all passages of one turn.
EXCERPT = 4000
TURN_CAP = 40000


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def file_version(path: Path) -> str:
    """The version string the indexer stores as `file_hash` (mtime, size, pipeline)."""
    st = path.stat()
    return f"{st.st_mtime:.0f}:{st.st_size}:v4"


def _index(col, rel: str) -> tuple[str, set[str]]:
    """The version the index holds for one file, and the hashes of its passages."""
    try:
        got = col.get(where={"source": rel}, include=["documents", "metadatas"])
    except Exception:
        log.debug("Index lookup failed for a cited file", exc_info=True)
        return "", set()
    metas = got.get("metadatas") or []
    version = next((m.get("file_hash", "") for m in metas if m and m.get("file_hash")), "")
    return version, {sha(d) for d in got.get("documents") or []}


def record(chunks: list[dict], col=None) -> list[dict]:
    """What a turn keeps about each passage it was given, in the order it was given them
    (so passage k is source k of the `sources` message)."""
    versions: dict[str, str] = {}
    budget = TURN_CAP
    out = []
    for c in chunks:
        wiki = c.get("type") == "wiki"
        text = c.get("text") or ""
        keep = text[:min(EXCERPT, budget)]
        budget -= len(keep)
        src = c.get("title") if wiki else c.get("source", "")
        if not wiki and col is not None and src not in versions:
            versions[src] = _index(col, src)[0]
        out.append({
            "source": src,
            "page": c.get("page", 0),
            "score": c.get("score", 0),
            "type": c.get("type", "chunk"),
            "heading_path": c.get("heading_path", ""),
            "page_type": c.get("page_type", ""),
            "sensitivity": c.get("sensitivity", ""),
            "text": keep,
            "trimmed": len(keep) < len(text),
            "sha": sha(text),
            "version": "" if wiki else versions.get(src, ""),
        })
    return out


def _state(cited: dict, group_id: str, username: str, open_collection) -> tuple[str, str]:
    """Where this passage stands now, and how to say it."""
    if cited.get("type") == "wiki":
        return "wiki", "A wiki page, written from the documents. Kept here as the answer saw it."
    if not get_group(group_id, username):
        return "gone", "This matter no longer exists. The passage below is the one the answer used."
    base = group_upload_dir(group_id, username).resolve()
    path = (base / cited.get("source", "")).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        return "gone", "This file is no longer in the matter. The passage below is the one the answer used."
    now = file_version(path)
    indexed, held = _index(open_collection(group_id, username), cited["source"])
    if now != indexed:
        return "changed", ("This file was edited after this answer and has not been read again. "
                           "The passage below is the one the answer used.")
    if cited["sha"] in held:
        if now == cited.get("version"):
            return "kept", ""
        return "same", "This file has been read again since this answer. It still holds this passage word for word."
    return "changed", ("This file has been read again since this answer and no longer holds this passage "
                       "word for word. It is kept here exactly as the answer was given it.")


def view(session: dict, n: int, k: int, username: str, open_collection) -> dict:
    """Passage k of turn n of a saved session, with what has become of its file.
    Raises LookupError when the session has no such turn or passage."""
    turns = session.get("turns") or []
    if not 0 <= n < len(turns):
        raise LookupError("This chat has no such answer")
    passages = turns[n].get("sources") or []
    if not 0 <= k < len(passages):
        raise LookupError("This answer did not cite that passage")
    cited = passages[k]
    if "sha" not in cited:
        return {**cited, "text": cited.get("text", ""), "state": "old",
                "note": "This answer was saved before passages were kept with it, so only its name and page remain."}
    group_id = session.get("group_id", "default")
    try:
        state, note = _state(cited, group_id, username, open_collection)
    except ValueError:
        state, note = "gone", "This matter no longer exists. The passage below is the one the answer used."
    return {**cited, "state": state, "note": note}
