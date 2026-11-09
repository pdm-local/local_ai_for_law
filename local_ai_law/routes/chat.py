"""
WebSocket chat endpoint.

Legal RAG flow:
  1. Retrieve relevant wiki, graph, and document context for the active group.
  2. Build a legal answer prompt with source-grounding and answer templates.
  3. Stream the local model response back to the browser.

Retrieval:
  - If wiki is enabled: wiki-first pipeline (FTS5 + graph + chunks).
  - Otherwise: classic BM25 + vector chunk retrieval.

Results are labeled [wiki] or [doc] in the context sent to the LLM.
"""

import asyncio
import json
import os
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any

from fastapi import APIRouter, WebSocket

from local_ai_law.config import (
    MAX_QUERY_CHARS, RATE_LIMIT_PER_MIN,
    DEFAULT_SETTINGS, DEFAULT_LANGUAGE,
    load_settings, get_group, user_history_dir, load_edition_config, log,
    ollama_keep_alive, atomic_write_json, safe_num_ctx, OLLAMA_HOST, OLLAMA_TIMEOUT,
    validate_safe_id,
)

import re as _re

from local_ai_law.models import TokenEstimateIn
from local_ai_law.services.indexer import get_group_collection
from local_ai_law.services.retrieval import retrieve_smart, retrieve_carded, named_docs
from local_ai_law.services.prompting import build_system_prompt, has_citation
from local_ai_law.services.lang import chat_model_for, guess_language
from local_ai_law.services.overview import load_overview
from local_ai_law.services import citations
from local_ai_law.services.audit import audit_log
from local_ai_law.services.metrics import ws_connect, ws_disconnect
from local_ai_law.services.ollama_client import chat as ollama_chat

# What the user is told when the local model can't answer. The cause (model name, Ollama
# address, the exception) goes to the log for the owner, never to the page.
UNAVAILABLE = "The assistant is not available right now. Please try again in a moment."

_META_DOC_RE = _re.compile(
    r'(?:^|\b)(list|show me|what are|which|give me|tell me|how many)\b.{0,30}'
    r'\b(document|file|paper|pdf|notebook|upload|index|matter material|source material)s?\b',
    _re.I,
)

router = APIRouter(tags=["chat"])


# ── Rate limiter ──────────────────────────────────────────────────────────────

class _RateLimiter:
    def __init__(self, max_per_min: int):
        self._max  = max_per_min
        self._times: list[float] = []

    def allow(self) -> bool:
        now = time.time()
        self._times = [t for t in self._times if now - t < 60]
        if len(self._times) >= self._max:
            return False
        self._times.append(now)
        return True


# ── LLM streaming ─────────────────────────────────────────────────────────────

async def stream_llm(
    ws: WebSocket,
    model: str,
    messages: list[dict],
    num_ctx: int = 8192,
    options: dict | None = None,
) -> tuple[str, dict[str, Any]]:
    """Stream an Ollama chat completion to the WebSocket token-by-token, hiding
    <think>…</think> reasoning from the client. The blocking Ollama iterator runs on a
    worker thread and feeds an asyncio queue. Returns (full_answer, stats)."""
    q: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _run():
        buf, in_think = "", False
        try:
            for chunk in ollama_chat(
                model=model, messages=messages, stream=True,
                num_ctx=num_ctx,
                options=options,
                keep_alive=ollama_keep_alive(),
            ):
                if chunk.get("done"):
                    loop.call_soon_threadsafe(q.put_nowait, ("stats", chunk))
                    continue
                raw = chunk.get("message", {}).get("content", "") or ""
                if not raw:
                    continue
                buf += raw
                emit = ""
                while True:
                    if not in_think:
                        s = buf.find("<think>")
                        if s == -1:
                            emit += buf[:-6] if len(buf) > 6 else ""
                            buf = buf[-6:] if len(buf) > 6 else buf
                            break
                        emit += buf[:s]
                        buf = buf[s + 7:]
                        in_think = True
                    else:
                        e = buf.find("</think>")
                        if e == -1:
                            buf = buf[-8:] if len(buf) > 8 else buf
                            break
                        buf = buf[e + 8:]
                        in_think = False
                if emit:
                    loop.call_soon_threadsafe(q.put_nowait, ("tok", emit))
        except Exception as e:
            loop.call_soon_threadsafe(q.put_nowait, ("err", str(e)))
        finally:
            if not in_think and buf.strip():
                loop.call_soon_threadsafe(q.put_nowait, ("tok", buf))
            loop.call_soon_threadsafe(q.put_nowait, ("end", ""))

    threading.Thread(target=_run, daemon=True).start()

    full, started = "", False
    stats: dict[str, Any] = {}
    started_at = time.time()
    while True:
        kind, val = await q.get()
        if kind == "end":
            break
        if kind == "err":
            # The owner reads the cause in the log; the user is told nothing technical.
            log.warning("Chat generation failed (Ollama at %s): %s", OLLAMA_HOST, val)
            val = UNAVAILABLE
            await ws.send_json({"type": "error", "content": val})
            stats["_error"] = val
            return full, stats
        if kind == "stats":
            stats = {
                "tokens_in": val.get("prompt_eval_count", 0),
                "tokens_out": val.get("eval_count", 0),
                "duration_ms": int(val.get("total_duration", 0) / 1e6) if val.get("total_duration") else 0,
                "eval_duration_ms": int(val.get("eval_duration", 0) / 1e6) if val.get("eval_duration") else 0,
            }
            continue
        if not started:
            await ws.send_json({"type": "start"})
            started = True
        await ws.send_json({"type": "chunk", "content": val})
        full += val
    if not stats.get("duration_ms"):
        stats["duration_ms"] = int((time.time() - started_at) * 1000)
    tokens_out = int(stats.get("tokens_out") or 0)
    eval_ms = int(stats.get("eval_duration_ms") or stats.get("duration_ms") or 0)
    stats["tokens_per_sec"] = round(tokens_out / (eval_ms / 1000), 1) if tokens_out and eval_ms else 0
    return full, stats



async def run_with_heartbeat(ws: WebSocket, coro, message: str):
    """Await a coroutine while sending a `status` heartbeat every 2s so the client UI
    shows progress (and the socket stays alive) during slow retrieval/generation."""
    task = asyncio.ensure_future(coro)
    start_time = time.time()
    while not task.done():
        done, pending = await asyncio.wait([task], timeout=2.0)
        if not done:
            elapsed = int(time.time() - start_time)
            try:
                await ws.send_json({"type": "status", "content": f"{message} ({elapsed}s)"})
            except Exception as e:
                task.cancel()
                raise e
    return task.result()


def _estimate_tokens(model: str, text: str) -> int:
    """Token count via Ollama's /api/tokenize; falls back to ~chars/4 on any error."""
    host = OLLAMA_HOST.rstrip("/")
    payload = json.dumps({"model": model, "prompt": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{host}/api/tokenize",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return len(data.get("tokens", [])) or max(1, len(text) // 4)
    except Exception:
        return max(1, len(text) // 4)


@router.post("/api/chat/token-estimate")
async def chat_token_estimate(body: TokenEstimateIn):
    """POST — estimate token usage of a pending chat turn vs the context limit (for the UI meter)."""
    parts: list[str] = []
    for msg in body.messages:
        role = str(msg.get("role", "")).strip()
        content = str(msg.get("content", "")).strip()
        if content:
            parts.append(f"{role}: {content}" if role else content)
    if body.pending_input.strip():
        parts.append(f"user: {body.pending_input.strip()}")
    text = "\n\n".join(parts)
    count = _estimate_tokens(body.model, text) if text else 0
    limit = int(body.num_ctx or DEFAULT_SETTINGS["num_ctx"])
    return {
        "count": count,
        "limit": limit,
        "percentage": round(count / limit * 100, 1) if limit else 0,
    }


# ── Session persistence ───────────────────────────────────────────────────────

def _save_session(session: dict, username: str):
    """Persist a chat session to the user's history dir (atomic write, file named by session_id)."""
    # session_id is server-generated, but validate before it becomes a filename so a
    # malformed/forged id can never escape the user's own history dir (path traversal).
    validate_safe_id(session["session_id"], "session_id")
    hist_dir = user_history_dir(username)
    hist_dir.mkdir(exist_ok=True)
    path = hist_dir / f"{session['session_id']}.json"
    atomic_write_json(path, session)


def _load_session(session_id: str, username: str) -> dict | None:
    """Load a saved session from disk. Returns None if not found.

    session_id is client-supplied (WS ?session_id= / resume_session). It is validated
    here so it can only ever name a file inside THIS user's history dir — a value like
    '../../otheruser/chat_history/x' is rejected, preventing cross-user chat disclosure.
    """
    try:
        validate_safe_id(session_id, "session_id")
    except ValueError:
        log.warning("Rejected unsafe session_id for user %s: %r", username, session_id)
        return None
    hist_dir = user_history_dir(username)
    path = hist_dir / f"{session_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            log.debug("Unreadable session file %s; scanning by id", path, exc_info=True)
    # Fallback: scan all files (handles sessions saved before filename=session_id)
    for f in hist_dir.glob("*.json"):
        try:
            s = json.loads(f.read_text())
            if s.get("session_id") == session_id:
                return s
        except Exception:
            log.debug("Skipping unreadable session file %s", f, exc_info=True)
    return None


def _history_from_session(session: dict) -> list[dict]:
    """Reconstruct LLM message history from saved session turns."""
    history: list[dict] = []
    for turn in session.get("turns", []):
        if turn.get("user"):
            history.append({"role": "user", "content": turn["user"]})
        if turn.get("assistant"):
            history.append({"role": "assistant", "content": turn["assistant"]})
    return history


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(chunks: list[dict]) -> str:
    """Format mixed wiki + chunk results for the LLM context."""
    parts = []
    for c in chunks:
        if c.get("type") == "wiki":
            priv = "[PRIVILEGED] " if c.get("sensitivity") in ("privileged", "sensitive") else ""
            label = f"{priv}[wiki: {c.get('title', c.get('source', '?'))} ({c.get('page_type', '')})]"
        else:
            hp = c.get("heading_path", "")
            section = f" § {hp}" if hp else ""
            label = f"[doc: {c['source']} p.{c['page']}{section} | score {c['score']}]"
        parts.append(f"{label}\n{c['text']}")
    return "\n\n---\n\n".join(parts)


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@router.websocket("/ws/chat")
async def chat_ws(ws: WebSocket):
    """The main chat WebSocket: authenticates the socket, then per `query` message runs
    retrieval → streams the grounded answer (`stream_llm`) → emits sources and persists the
    session. Also handles ping/clear_history/resume_session control messages."""
    from local_ai_law.services.auth import verify_token, user_count

    username = "default"
    is_admin = True
    if user_count() > 0:
        # Try query param first (existing clients), then httpOnly cookie
        token = ws.query_params.get("token", "")
        user_info = verify_token(token) if token else None
        if not user_info:
            cookie_token = ws.cookies.get("rag_token", "")
            user_info = verify_token(cookie_token) if cookie_token else None
        if not user_info:
            await ws.close(code=4001, reason="Not authenticated")
            return
        username = user_info["sub"]
        is_admin = user_info.get("role") == "admin"

    # Origin validation
    origin = ws.headers.get("origin", "")
    host   = ws.headers.get("host", "")
    if origin:
        from urllib.parse import urlparse
        o = urlparse(origin)
        allowed = {host, "localhost", "127.0.0.1", "0.0.0.0", host.split(":")[0]}
        if o.hostname not in allowed and f"{o.hostname}:{o.port}" not in allowed:
            log.warning("WS rejected origin: %s (host: %s)", origin, host)
            await ws.close(code=4003, reason="Origin not allowed")
            return

    # Instance lockout + license gate. The HTTP middleware skips WebSocket upgrade
    # requests, so these must be enforced here (before accept) or a locked/unlicensed
    # instance would still serve chat over /ws/chat. No-op when not locked / licensing
    # disabled (no public key configured).
    from local_ai_law.services.license import (
        is_locked, is_licensing_enabled, load_user_license)
    if is_locked():
        await ws.close(code=4423, reason="Instance is locked")
        return
    if user_count() > 0 and is_licensing_enabled() and not load_user_license(username):
        await ws.close(code=4402, reason="No valid license")
        return

    await ws.accept()
    ws_connect()
    limiter = _RateLimiter(RATE_LIMIT_PER_MIN)

    session: dict = {}
    history: list[dict] = []
    cur_group_id: str | None = None
    col = None
    group = None
    edition_cfg: dict = {}
    indexed_count = 0

    # Settings cache — reload from disk at most once every 30s
    _cfg_cache: dict = {}
    _cfg_loaded_at: float = 0.0

    def _get_cfg() -> dict:
        nonlocal _cfg_cache, _cfg_loaded_at
        if time.time() - _cfg_loaded_at > 30:
            _cfg_cache = load_settings(username)
            _cfg_loaded_at = time.time()
        return _cfg_cache

    # ── Resume a previous session if session_id provided ─────────────────────
    resume_id = ws.query_params.get("session_id", "")
    if resume_id:
        saved = _load_session(resume_id, username)
        if saved:
            session       = saved
            history       = _history_from_session(saved)
            cur_group_id  = saved.get("group_id", "default")
            group         = get_group(cur_group_id, username)
            edition_cfg   = load_edition_config()
            try:
                col = get_group_collection(cur_group_id, username)
            except Exception:
                col = None
            await ws.send_json({
                "type":       "session_resumed",
                "session_id": resume_id,
                "group_id":   cur_group_id,
                "turns":      len(history) // 2,
                "history":    [
                    {"role": t["role"], "content": t["content"][:300]}
                    for t in history[-10:]   # last 5 turns (10 messages: user+assistant pairs)
                ],
            })
        else:
            await ws.send_json({
                "type":    "warning",
                "content": f"Session '{resume_id}' not found. Starting a new session.",
            })

    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type", "query")

            if t == "ping":
                await ws.send_json({"type": "pong"})
                continue

            if t == "clear_history":
                history.clear()
                await ws.send_json({"type": "history_cleared"})
                continue

            if t == "resume_session":
                rid = data.get("session_id", "")
                saved = _load_session(rid, username) if rid else None
                if saved:
                    session      = saved
                    history      = _history_from_session(saved)
                    cur_group_id = saved.get("group_id", "default")
                    group        = get_group(cur_group_id, username)
                    edition_cfg  = load_edition_config()
                    try:
                        col = get_group_collection(cur_group_id, username)
                    except Exception:
                        col = None
                    await ws.send_json({
                        "type":       "session_resumed",
                        "session_id": rid,
                        "group_id":   cur_group_id,
                        "turns":      len(history) // 2,
                        "history":    [
                            {"role": t["role"], "content": t["content"][:300]}
                            for t in history[-10:]
                        ],
                    })
                else:
                    await ws.send_json({"type": "error", "content": f"Session '{rid}' not found"})
                continue

            if not limiter.allow():
                await ws.send_json({"type": "error", "content": "Rate limit exceeded."})
                continue

            new_group_id = data.get("group_id", "default")
            if new_group_id != cur_group_id:
                if session.get("turns"):
                    _save_session(session, username)
                cur_group_id = new_group_id
                history.clear()
                group = get_group(cur_group_id, username)
                edition_cfg = load_edition_config()

                session = {
                    "session_id": datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f") + f"_{os.urandom(4).hex()}",
                    "started":    datetime.now().isoformat(),
                    "group_id":   cur_group_id,
                    "group_name": group["name"] if group else cur_group_id,
                    "model":      "",
                    "top_k":      10,
                    "turns":      [],
                }

                try:
                    col = get_group_collection(cur_group_id, username)
                    indexed_count = int(col.count() or 0)
                    if indexed_count == 0:
                        await ws.send_json({
                            "type":    "warning",
                            "content": "This group has no indexed documents yet. I will answer without document context.",
                        })
                except Exception as e:
                    log.warning("WS chat: could not open the index of %s: %s", cur_group_id, e)
                    await ws.send_json({
                        "type": "warning",
                        "content": "This client's documents can't be searched right now; answering without them.",
                    })
                    col = None
                    indexed_count = 0

            query = data.get("query", "").strip()
            client_query_id = data.get("client_query_id", "")
            # image_data: single base64 string (legacy) or list of base64 strings
            _raw_img = data.get("image_data", "")
            if isinstance(_raw_img, list):
                image_list: list[str] = [x for x in _raw_img if x]
            else:
                image_list = [_raw_img] if _raw_img else []
            if not query and not image_list:
                continue
            if not query:
                query = "Describe and explain what you see in this image."
            if len(query) > MAX_QUERY_CHARS:
                await ws.send_json({"type": "error", "content": f"Query too long (max {MAX_QUERY_CHARS} chars)"})
                continue
            log.info(
                "WS chat query received user=%s group=%s model=%s chars=%s images=%s client_query_id=%s",
                username,
                data.get("group_id", "default"),
                data.get("model", ""),
                len(query),
                len(image_list),
                client_query_id,
            )
            await ws.send_json({
                "type": "query_received",
                "client_query_id": client_query_id,
                "group_id": data.get("group_id", "default"),
            })
            if col is not None:
                try:
                    indexed_count = int(col.count() or 0)
                except Exception:
                    indexed_count = 0

            cfg      = _get_cfg()
            # The model, top-k and level come from the server's settings (the owner's .env).
            # Only an admin's client (the bench scripts) may override them per query.
            override = data if is_admin else {}
            # Per-group language wins; otherwise fall back to the global app language. It is
            # the answer language, and it picks the chat model: `chat_model_nl` /
            # `chat_model_en` when set (a model trained in that language only), else
            # `chat_model`.
            group_lang  = (group or {}).get("language") or cfg.get("app_language") or DEFAULT_LANGUAGE
            model    = override.get("model") or chat_model_for(cfg, group_lang)
            if not model:
                log.warning("WS chat: no chat model configured (set RAG_CHAT_MODEL in .env)")
                await ws.send_json({"type": "error", "content": UNAVAILABLE})
                continue
            # Multi-doc reasoning turns (compare / connection / contradiction) can route to
            # a stronger local synthesis model (`reasoning_model`, "" = off), and so can a
            # turn whose retrieval finds same-kind sibling documents (below). Other single-doc
            # turns stay on the fast chat model so latency/the live demo is unaffected. The
            # override is per-turn only — `session["model"]` keeps the user's base model so
            # it never leaks into later turns.
            base_model = model
            _reasoning_model = (cfg.get("reasoning_model") or "").strip()
            if _reasoning_model and _reasoning_model != model:
                from local_ai_law.services.answer_templates import is_multidoc
                if is_multidoc(query):
                    model = _reasoning_model
                    await ws.send_json({"type": "status",
                                        "content": "Reading across several documents..."})
            top_k    = override.get("top_k") or cfg["top_k"]
            wiki_top_k = max(1, min(int(cfg.get("wiki_top_k", 3)), 20))
            em       = cfg["embed_model"]
            num_ctx  = safe_num_ctx(cfg.get("num_ctx", DEFAULT_SETTINGS["num_ctx"]))
            gen_options = {
                "temperature":    cfg.get("temperature",    DEFAULT_SETTINGS.get("temperature")),
                "top_p":          cfg.get("top_p",          DEFAULT_SETTINGS.get("top_p")),
                "repeat_penalty": cfg.get("repeat_penalty", DEFAULT_SETTINGS.get("repeat_penalty")),
            }
            difficulty  = override.get("difficulty") or cfg.get("difficulty", "intermediate")

            session["model"] = base_model
            session["top_k"] = top_k

            # The documents the user chose for this chat (the Documents dialog). None means
            # all of them. The collection is the client's own, so this can only narrow.
            _src = data.get("sources")
            sources = ([x for x in _src if isinstance(x, str) and x][:2000]
                       if isinstance(_src, list) else []) or None
            # A question that names a document by file name or full title is answered
            # from that document only (within the chosen ones), so a same-matter sibling
            # that says the opposite cannot answer for it.
            try:
                _named = named_docs(query, cur_group_id, username)
            except Exception:
                log.debug("named_docs failed", exc_info=True)
                _named = []
            if sources:
                _named = [d for d in _named if d in sources]
            if _named:
                sources = _named
                await ws.send_json({"type": "status",
                                    "content": "Answering from: " + ", ".join(_named)})

            # The client overview speaks for every document, so it stays out of a chat
            # limited to some of them.
            overview = None if sources else load_overview(cur_group_id, username)
            # build_system_prompt injects the language rule (top + bottom) itself.
            sysp = build_system_prompt(
                cfg, edition_cfg, query,
                group_lang=group_lang, difficulty=difficulty, overview=overview,
            )

            # Build retrieval query from conversation context
            retrieval_query = query
            if history:
                recent_user = [h["content"] for h in history[-10:] if h["role"] == "user"]
                if recent_user:
                    joined = " ".join(recent_user[-3:] + [query])
                    if len(joined) > 700:
                        # Truncate at word boundary
                        cut = joined[:700].rsplit(" ", 1)[0]
                        joined = cut if cut else joined[:700]
                    retrieval_query = joined

            # ── 0. Meta-query: full document manifest injection ───────────────
            meta_doc_context = ""
            if _META_DOC_RE.search(query):
                try:
                    from local_ai_law.config import group_doc_paths, group_upload_dir
                    from local_ai_law.services.graph import list_wiki_pages
                    _grp_obj = get_group(cur_group_id, username)
                    _all_paths = group_doc_paths(cur_group_id, _grp_obj or {}, username) if _grp_obj else []
                    _ov = load_overview(cur_group_id, username) or {}
                    _desc_map = {d["name"]: d.get("description", "") for d in _ov.get("documents", [])}
                    _ideas_map = {d["name"]: d.get("key_ideas", []) for d in _ov.get("documents", [])}
                    _base = group_upload_dir(cur_group_id, username)
                    doc_lines = []
                    if sources:
                        _keep = set(sources)
                        _all_paths = [p for p in _all_paths
                                      if p.is_relative_to(_base)
                                      and p.relative_to(_base).as_posix() in _keep]
                    for p in _all_paths:
                        # Overview keys documents by their group-relative name; look
                        # up by rel path (fall back to basename for legacy overviews).
                        try:
                            rel = p.relative_to(_base).as_posix()
                        except Exception:
                            rel = p.name
                        desc = _desc_map.get(rel) or _desc_map.get(p.name, "")
                        ideas = _ideas_map.get(rel) or _ideas_map.get(p.name, [])
                        line = f"  • {rel}"
                        if desc:
                            line += f" — {desc}"
                        if ideas:
                            line += "\n    Key ideas: " + "; ".join(ideas)
                        doc_lines.append(line)
                    _wiki_pages = [] if sources else list_wiki_pages(username, cur_group_id)
                    wiki_lines = [f"  • [{p['page_type']}] {p['title']}" for p in _wiki_pages]
                    meta_doc_context = (
                        f"COMPLETE DOCUMENT MANIFEST FOR THIS MATTER ({len(doc_lines)} files):\n"
                        + "\n".join(doc_lines)
                        + (f"\n\nWIKI PAGES ({len(wiki_lines)} pages):\n" + "\n".join(wiki_lines) if wiki_lines else "")
                        + "\n\n(This is the authoritative list — mention every item above.)"
                    )
                except Exception:
                    log.debug("Could not build document manifest context", exc_info=True)

            # ── 1. Retrieve (wiki-first or fallback) ──────────────────────────
            chunks: list[dict] = []
            doc_summaries: list[dict] = []
            siblings: list[str] = []
            if col is not None and indexed_count > 0:
                await ws.send_json({"type": "status", "content": "Searching knowledge base..."})
                try:
                    sensitivity_max = "sensitive"
                    if edition_cfg.get("sensitivity_enforcement"):
                        from local_ai_law.services.auth import _load_users
                        users = _load_users().get("users", [])
                        role = next((u["role"] for u in users if u["username"] == username), "user")
                        sensitivity_max = "sensitive" if role == "admin" else "internal"

                    use_wiki = (
                        edition_cfg.get("wiki_enabled", True)
                        and cfg.get("wiki_first_retrieval", True)
                        and not sources
                    )

                    # Document router: pick the relevant doc(s) then fetch a
                    # small, focused set of chunks. Skipped for meta/"list all
                    # documents" queries, which need the whole manifest.
                    use_router = cfg.get("retrieval_router", True) and not meta_doc_context

                    if use_router:
                        # Document-card retrieval (search whole-doc cards → pick
                        # → chunks from picked); falls back to chunk-level routing
                        # internally if a group has no card index yet.
                        coro = asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: retrieve_carded(
                                retrieval_query, col, em,
                                username, cur_group_id,
                                settings=cfg, sources=sources,
                            ),
                        )
                    else:
                        coro = asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: retrieve_smart(
                                retrieval_query, col, top_k, em,
                                username, cur_group_id,
                                wiki_top_k=wiki_top_k,
                                sensitivity_max=sensitivity_max,
                                use_wiki=use_wiki,
                                settings=cfg, sources=sources,
                            ),
                        )
                    res = await run_with_heartbeat(ws, coro, "Searching knowledge base...")
                    chunks = res["results"]
                    doc_summaries = res.get("doc_summaries") or []
                    siblings = res.get("siblings") or []
                    # Same-kind documents compete for the answer: the small model takes
                    # the first passage it sees, so this turn goes to the reasoning model
                    # too (measured on the sandbox: the 35B named memo-018, the 4B did not).
                    if (siblings and _reasoning_model and model == base_model
                            and _reasoning_model != model):
                        model = _reasoning_model
                        await ws.send_json({"type": "status",
                                            "content": "Reading similar documents side by side..."})
                    if res.get("routed_docs"):
                        await ws.send_json({"type": "status",
                                            "content": "Routed to: " + ", ".join(res["routed_docs"])})
                    if res.get("second_pass"):
                        await ws.send_json({"type": "status", "content": "Refining search with expanded terms..."})
                except Exception as e:
                    log.warning("WS chat: retrieval failed: %s", e, exc_info=True)
                    await ws.send_json({
                        "type": "warning",
                        "content": "The documents can't be searched right now; answering without them.",
                    })
                    chunks = []
            else:
                await ws.send_json({"type": "status", "content": "Generating without document context..."})

            # Whole-doc summaries of the routed docs (Fix: cross-doc synthesis). Prepended
            # to the context so the model sees each doc's gist, not just a chunk sliver —
            # and so it survives the token-budget chunk-dropping below (it's the small,
            # high-value part). Gated by `answer_inject_summaries`.
            summary_block = ""
            if cfg.get("answer_inject_summaries", True) and doc_summaries:
                _sl = [f"- {d['title']}: {d['focus']}" for d in doc_summaries]
                summary_block = (
                    "DOCUMENT SUMMARIES (routed documents — use these to connect facts "
                    "across documents; cite the specific source for each fact):\n"
                    + "\n".join(_sl)
                )

            # Same-kind documents of one matter (retrieve_carded `siblings`): the model is
            # told they may disagree, so it answers from the one the question points to,
            # or sets them side by side, instead of taking the first sibling for the answer.
            sibling_block = ""
            if siblings:
                _by = {d["name"]: d["title"] for d in doc_summaries}
                sibling_block = (
                    "DOCUMENTS OF THE SAME KIND IN THIS MATTER (they may not agree):\n"
                    + "\n".join(f"- {n}" + (f": {_by[n]}" if n in _by else "") for n in siblings)
                    + "\nAnswer from the document the question points to (by its title, author, "
                    "recipient, date or wording) and name it. If they say different things on "
                    "the point asked, say what each one says and cite both; never blend them."
                )

            def _ctx_from(chunk_list):
                c = _build_context(chunk_list)
                if meta_doc_context:
                    c = meta_doc_context + ("\n\n---\n\n" + c if c else "")
                if summary_block:
                    c = summary_block + ("\n\n---\n\n" + c if c else "")
                if sibling_block:
                    c = sibling_block + ("\n\n---\n\n" + c if c else "")
                return c

            context = _ctx_from(chunks)

            # Token-aware context budget: reserve room for the system prompt, chat
            # history and the answer so context can never crowd out generation.
            # (The old chars*4 heuristic mis-estimated tokens and cut mid-text.)
            _OUTPUT_RESERVE = 1024
            _hist_text = "\n".join(str(m.get("content", "")) for m in history[-10:]) if history else ""
            _overhead = _estimate_tokens(model, sysp) + _estimate_tokens(model, _hist_text)
            _ctx_budget_tok = max(512, num_ctx - _overhead - _OUTPUT_RESERVE)

            if context and _estimate_tokens(model, context) > _ctx_budget_tok:
                # Drop the lowest-scored chunks first until the context fits.
                kept = sorted(chunks, key=lambda c: c.get("score", 0), reverse=True)
                while kept:
                    trial = _ctx_from(kept)
                    if _estimate_tokens(model, trial) <= _ctx_budget_tok:
                        context = trial
                        break
                    kept.pop()
                else:
                    # Nothing fits (e.g. a huge meta manifest) — hard-trim as a last resort.
                    context = (meta_doc_context or context)[: _ctx_budget_tok * 4]
                chunks = kept

            # ── 1b. Vision analysis (sequential — one GPU slot at a time) ───────
            vision_parts: list[str] = []
            if image_list:
                from local_ai_law.services.vision import analyze_image
                vision_model = cfg.get("vision_model", "")
                if not vision_model:
                    log.warning("WS chat: image sent but no vision model configured (RAG_VISION_MODEL)")
                    await ws.send_json({"type": "error", "content": "Images can't be read right now."})
                    continue
                n = len(image_list)
                # Vision models need less context than chat — cap at 2048 to stay fast
                # and avoid wasting VRAM on a model that processes a single image.
                vision_ctx = min(int(cfg.get("num_ctx", 4096)), 2048)
                for idx, img_b64 in enumerate(image_list, 1):
                    label = f"image {idx}/{n}" if n > 1 else "image"
                    await ws.send_json({"type": "status", "content": f"Reading {label}…"})
                    try:
                        _img = img_b64  # capture for lambda
                        coro = asyncio.get_running_loop().run_in_executor(
                            None,
                            lambda: analyze_image(_img, query, vision_model, vision_ctx),
                        )
                        v_result = await asyncio.wait_for(
                            run_with_heartbeat(ws, coro, f"Reading {label}…"),
                            timeout=OLLAMA_TIMEOUT,
                        )
                        prefix = f"[IMAGE {idx} — type: {v_result.image_type}]" if n > 1 else f"[IMAGE — type: {v_result.image_type}]"
                        vision_parts.append(f"{prefix}\n{v_result.description}")
                        await ws.send_json({
                            "type": "vision_result",
                            "index": idx,
                            "total": n,
                            "image_type": v_result.image_type,
                            "is_code": v_result.is_code,
                            "snippet": v_result.description[:200],
                        })
                    except asyncio.TimeoutError:
                        await ws.send_json({"type": "warning", "content": f"Image {idx} analysis timed out ({OLLAMA_TIMEOUT}s) — skipping."})
                    except Exception as e:
                        log.warning("WS chat: image %s analysis failed: %s", idx, e)
                        await ws.send_json({"type": "warning", "content": f"Image {idx} could not be read, so it is skipped."})

            # Prepend all vision analyses to context so the LLM sees them first
            if vision_parts:
                vision_context = "\n\n".join(vision_parts)
                context = f"{vision_context}\n\n---\n\n{context}" if context else vision_context

            # ── 2. Chat over retrieved context ────────────────────────────────
            # Product decision (intentional, not a bug): when retrieval returns no
            # document context we do NOT hard-refuse. The assistant answers from the
            # local model's general legal knowledge but is explicitly instructed to flag
            # that the answer is not grounded in the user's uploaded documents. This keeps
            # the tool useful for general questions while never passing off ungrounded
            # output as document-based. (To switch to strict refuse-when-no-docs, replace
            # the else-branch system/user prompts below with a refusal instruction.)
            agent_type = "chat"
            if context:
                user_msg = f"Context:\n{context}\n\n---\nQuestion: {query}"
                effective_sysp = sysp
            else:
                effective_sysp = (
                    sysp
                    + "\n\nFor turns with no retrieved document context, answer from the local model's general knowledge "
                    "instead of refusing solely because context is empty. Clearly say when the answer is not grounded in uploaded documents."
                )
                user_msg = (
                    "No document context was retrieved for this turn. "
                    "Answer from your general local model knowledge and be clear when the answer is not grounded in uploaded documents.\n\n"
                    f"Question: {query}"
                )
            msgs = [{"role": "system", "content": effective_sysp}]
            msgs += history[-10:]
            msgs += [{"role": "user", "content": user_msg}]

            await ws.send_json({"type": "status", "content": "Generating response..."})
            full, gen_stats = await stream_llm(ws, model, msgs, num_ctx=num_ctx, options=gen_options)

            if not full:
                if not gen_stats.get("_error"):
                    await ws.send_json({"type": "error", "content": "No answer came back. Please try again."})
                continue

            # ── 3. Send sources ───────────────────────────────────────────────
            await ws.send_json({
                "type": "sources",
                "sources": [
                    {
                        "type":   c.get("type", "chunk"),
                        "source": c.get("title") if c.get("type") == "wiki" else c["source"],
                        "page":   c.get("page", 0),
                        "score":  c["score"],
                        "text":   c["text"][:250],
                        "page_type": c.get("page_type", ""),
                        "heading_path": c.get("heading_path", ""),
                        "sensitivity": c.get("sensitivity", ""),
                    }
                    for c in chunks
                ],
                "agent": agent_type,
                # Where this turn's passages are kept: GET /api/sessions/{id}/turns/{n}/passages/{k}
                "session_id": session["session_id"],
                "turn": len(session["turns"]),
            })

            # ── 3b. Citation post-check (I-2) ─────────────────────────────────
            # If we grounded on real documents (not just wiki), nudge when the
            # answer names no source/page, or when the retrieved passages carry no
            # page/heading metadata to cite from. Best-effort — never blocks the turn.
            doc_chunks = [c for c in chunks if c.get("type") != "wiki"]
            if doc_chunks and not has_citation(full):
                await ws.send_json({
                    "type": "warning",
                    "content": "This answer cites no document or page — verify it against the sources below.",
                })
            # An answer in the other language goes to the log (the owner's signal that the
            # model drifts); the user still gets it.
            said = guess_language(full)
            if said and said != group_lang:
                log.warning("WS chat answer in %s, expected %s (model=%s)", said, group_lang, model)
            if doc_chunks and not any(c.get("page") or c.get("heading_path") for c in doc_chunks):
                await ws.send_json({
                    "type": "warning",
                    "content": "Retrieved passages carry no page/section metadata; citations may be imprecise. Re-indexing the matter improves this.",
                })

            await ws.send_json({"type": "done", "stats": gen_stats})

            history.append({"role": "user",      "content": query})
            history.append({"role": "assistant",  "content": full})
            session["turns"].append({
                "timestamp": datetime.now().isoformat(),
                "user":      query,
                "assistant": full,
                "agent":     agent_type,
                # Each passage word for word, so a citation still shows what the answer
                # stood on after a reindex or an edit (services/citations.py).
                "sources": citations.record(chunks, col),
            })
            # Saved per turn, not only on disconnect: a citation opens from the file.
            _save_session(session, username)
            audit_log(username, "chat", target=cur_group_id,
                      details=f"[{agent_type}{'|vision' if image_list else ''}] {query[:200]}")

    except asyncio.CancelledError:
        pass
    except Exception as e:
        from starlette.websockets import WebSocketDisconnect
        # Starlette raises RuntimeError (not WebSocketDisconnect) when we send
        # after the client has gone — same benign condition, keep it quiet.
        _gone = isinstance(e, RuntimeError) and (
            "close message" in str(e) or "not connected" in str(e)
            or "websocket.send" in str(e)
        )
        if isinstance(e, WebSocketDisconnect) or _gone:
            log.debug("WS client disconnected: %s", e)
        else:
            log.error("WS chat error: %s", e, exc_info=True)
    finally:
        ws_disconnect()
        if session.get("turns"):
            _save_session(session, username)
