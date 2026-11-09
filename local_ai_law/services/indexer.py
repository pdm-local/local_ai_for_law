"""Background indexing logic  -  per group per user, runs in thread."""

import threading
import time
from datetime import datetime
from typing import Optional

import chromadb

from local_ai_law.config import (
    user_db_path, load_settings, get_group, group_doc_paths,
    group_upload_dir, DEFAULT_SETTINGS, log,
)
from local_ai_law.services.extraction import extract_file
from local_ai_law.services.chunking import split_chunks, chunk_id, chunk_ok
from local_ai_law.services.embedding import embed_texts
from local_ai_law.services.citations import file_version
from local_ai_law.services.overview import generate_group_overview
from local_ai_law.services.doc_summary import summarize_all_docs, load_doc_summaries


def _run_vision_ocr(raw: list[dict], vision_model: str, st=None) -> list[dict]:
    """Replace image-page placeholders with actual vision-model descriptions."""
    from local_ai_law.services.vision import analyze_image
    out = []
    for item in raw:
        if st is not None:
            with st._lock:
                if st.cancel_requested:
                    return out
        if not item.get("image_b64"):
            out.append(item)
            continue
        try:
            result = analyze_image(item["image_b64"], "Describe this page in full detail for indexing.", vision_model)
            new_text = result.description.strip()
            if new_text:
                item = {**item, "text": new_text}
        except Exception as e:
            log.warning("Vision OCR failed for %s page %s: %s", item.get("source"), item.get("page"), e)
        out.append(item)
    return out


# ── collection helpers ───────────────────────────────────────────────────────

def safe_coll_name(group_id: str) -> str:
    """Chroma-safe collection name for a group (prefix + sanitized, length-capped id)."""
    return "grp_" + group_id.replace("-", "_")[:40]


def get_group_collection(group_id: str, username: str = "default", reset: bool = False) -> chromadb.Collection:
    """Open (or create) the group's Chroma chunk collection; `reset=True` drops it first."""
    client = chromadb.PersistentClient(path=str(user_db_path(username)))
    name = safe_coll_name(group_id)
    if reset:
        try:
            client.delete_collection(name)
        except Exception:
            log.debug("No existing collection %s to delete on reset", name, exc_info=True)
    return client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})


def group_chunk_stats(group_id: str, username: str = "default") -> dict[str, int]:
    """Per-source chunk counts for a group (how many embedded chunks each document has)."""
    try:
        col = get_group_collection(group_id, username)
        metas = col.get()["metadatas"]
        out: dict[str, int] = {}
        for m in metas:
            s = m.get("source", "?")
            out[s] = out.get(s, 0) + 1
        return out
    except Exception:
        return {}


# ── indexing status (per group) ──────────────────────────────────────────────

class IdxStatus:
    def __init__(self):
        self.is_indexing      = False
        self.cancel_requested = False
        self.total_files      = 0
        self.done_files       = 0
        self.skipped_files    = 0
        self.total_chunks     = 0
        self.done_chunks      = 0
        self.current_file     = ""
        self.phase            = ""
        self.error            = ""
        self.last_completed: Optional[str] = None
        self.last_indexed_count: int = 0
        self.last_skipped_count: int = 0
        self.started_at: Optional[float] = None   # time.monotonic()
        # Per-file progress of the current run, {rel_path: 0..100}: reading a file
        # takes it to 50, embedding its pieces fills the rest, 100 means chat can use it.
        self.file_progress: dict[str, int] = {}
        self._lock = threading.Lock()

    def to_dict(self):
        import time
        eta_sec = None
        elapsed_sec = None
        if self.started_at is not None:
            elapsed_sec = round(time.monotonic() - self.started_at)
        if (self.phase == "Generating quick-look summaries..." and
                self.total_files > 0 and self.done_files > 0 and
                elapsed_sec is not None):
            # ETA based on per-doc rate during summarisation phase
            rate = self.done_files / max(elapsed_sec, 1)
            remaining = self.total_files - self.done_files
            eta_sec = round(remaining / rate) if rate > 0 else None
        elif (self.total_chunks > 0 and self.done_chunks > 0 and
              elapsed_sec is not None and
              self.phase not in ("Generating quick-look summaries...", "Building search index...")):
            rate = self.done_chunks / max(elapsed_sec, 1)
            remaining = self.total_chunks - self.done_chunks
            eta_sec = round(remaining / rate) if rate > 0 else None
        return {
            "is_indexing":       self.is_indexing,
            "cancel_requested":  self.cancel_requested,
            "total_files":    self.total_files,
            "done_files":     self.done_files,
            "skipped_files":  self.skipped_files,
            "total_chunks":   self.total_chunks,
            "done_chunks":    self.done_chunks,
            "current_file":   self.current_file,
            "phase":          self.phase,
            "error":          self.error,
            "last_completed": self.last_completed,
            "last_indexed_count": self.last_indexed_count,
            "last_skipped_count": self.last_skipped_count,
            "elapsed_sec":    elapsed_sec,
            "eta_sec":        eta_sec,
            "file_progress":  dict(self.file_progress),
        }


_idx_statuses: dict[str, IdxStatus] = {}


def idx_status(group_id: str, username: str = "default") -> IdxStatus:
    """The shared (per user+group) IdxStatus object tracking live indexing progress."""
    key = f"{username}:{group_id}"
    if key not in _idx_statuses:
        _idx_statuses[key] = IdxStatus()
    return _idx_statuses[key]


# ── main indexing function ───────────────────────────────────────────────────

def do_index_group(group_id: str, username: str = "default", reset: bool = False):
    """Index a group end-to-end: extract → chunk → embed every document into Chroma,
    then (re)build doc summaries/cards. `reset` re-embeds everything; otherwise only
    new/changed files (skip-hash). Runs as a LOW-pool job; updates idx_status as it goes."""
    group = get_group(group_id, username)
    if not group:
        return
    cfg = load_settings(username)
    st = idx_status(group_id, username)

    with st._lock:
        st.is_indexing      = True
        st.cancel_requested = False
        st.phase            = "Extracting & embedding..."
        st.error            = ""
        st.done_files       = 0
        st.skipped_files    = 0
        st.done_chunks      = 0
        st.total_chunks     = 0
        st.started_at       = time.monotonic()
        st.file_progress    = {}

    try:
        col = get_group_collection(group_id, username, reset=reset)
        paths = group_doc_paths(group_id, group, username)

        with st._lock:
            st.total_files = len(paths)

        size         = cfg.get("chunk_size", 400)
        overlap      = cfg.get("chunk_overlap", 60)
        em           = cfg.get("embed_model", DEFAULT_SETTINGS["embed_model"])
        vision_model = cfg.get("vision_model") or cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
        upload_base  = group_upload_dir(group_id, username)
        summaries    = load_doc_summaries(group_id, username)

        _all = col.get()
        existing_ids: set[str] = set(_all["ids"])
        by_source: dict[str, set[str]] = {}
        existing_hashes: dict[str, str] = {}
        for id_, meta in zip(_all["ids"], _all["metadatas"]):
            src = meta.get("source", "")
            by_source.setdefault(src, set()).add(id_)
            if src and "file_hash" in meta and src not in existing_hashes:
                existing_hashes[src] = meta["file_hash"]

        current_names = {p.relative_to(upload_base).as_posix() for p in paths}
        for dead_src, dead_ids in by_source.items():
            if dead_src and dead_src not in current_names:
                col.delete(ids=list(dead_ids))
                existing_ids -= dead_ids
                log.info("Removed %d orphaned chunks for: %s", len(dead_ids), dead_src)

        seen_ids: set[str] = set(existing_ids)
        all_new: list[dict] = []
        dup_dropped = 0

        for i, path in enumerate(paths):
            rel_source = path.relative_to(upload_base).as_posix()
            with st._lock:
                if st.cancel_requested:
                    st.phase = "Cancelled"
                    return
                st.current_file = rel_source
                st.done_files = i

            # Skip extraction entirely if file is unchanged.
            # The pipeline suffix in `file_version` (extraction + embedding model): bump it
            # to force re-extraction/re-embedding of files indexed by older versions
            # — required when the embedding model changes (vector dimensions differ).
            # Saved citations compare against the same string (services/citations.py).
            current_hash = file_version(path)
            if (not reset
                    and by_source.get(rel_source)
                    and existing_hashes.get(rel_source) == current_hash):
                log.debug("Skipping unchanged file: %s", rel_source)
                with st._lock:
                    st.skipped_files += 1
                    st.file_progress[rel_source] = 100
                continue

            raw = extract_file(path, table_format=cfg.get("table_format", "markdown"))
            for item in raw:
                item["source"] = rel_source
            if cfg.get("extraction_mode", "markdown") == "plain":
                raw = [item for item in raw if not item.get("image_b64")]
            elif any(item.get("image_b64") for item in raw):
                with st._lock:
                    st.phase = f"Vision OCR: {rel_source}"
                raw = _run_vision_ocr(raw, vision_model, st)
                with st._lock:
                    if st.cancel_requested:
                        st.phase = "Cancelled"
                        return
                    st.phase = "Extracting & embedding..."
            cks = [c for c in split_chunks(raw, size, overlap) if chunk_ok(c["text"])]
            for idx, c in enumerate(cks):
                c["chunk_index"] = idx

            doc_summary = summaries.get(rel_source) or {}
            summary_text = (doc_summary.get("tldr") or "").strip()[:120]
            for c in cks:
                page = c.get("page", "")
                prefix = f"[Doc: {rel_source} | Page: {page}]"
                hp = c.get("heading_path", "")
                if hp:
                    prefix = f"[Doc: {rel_source} | Page: {page} | Section: {hp}]"
                if summary_text:
                    prefix += f" {summary_text}"
                c["text"] = f"{prefix}\n\n{c['text']}"

            unique_cks: list[dict] = []
            file_ids: set[str] = set()
            for c in cks:
                cid = chunk_id(c)
                if cid in file_ids:
                    dup_dropped += 1
                    continue
                file_ids.add(cid)
                c["file_hash"] = current_hash
                unique_cks.append(c)

            stale = list(by_source.get(rel_source, set()) - file_ids)
            if stale:
                col.delete(ids=stale)
                existing_ids -= set(stale)
                seen_ids -= set(stale)

            for c in unique_cks:
                cid = chunk_id(c)
                if cid in seen_ids:
                    dup_dropped += 1
                    continue
                seen_ids.add(cid)
                all_new.append(c)
            with st._lock:
                st.file_progress[rel_source] = 50

        # Pieces still to embed per file, so each file's bar fills as its own pieces land.
        file_left: dict[str, int] = {}
        file_total: dict[str, int] = {}
        for c in all_new:
            file_total[c["source"]] = file_total.get(c["source"], 0) + 1
        file_left.update(file_total)
        with st._lock:
            for rel in st.file_progress:
                if rel not in file_total:
                    st.file_progress[rel] = 100

        if dup_dropped:
            log.info("Dropped %d duplicate chunks during dedup", dup_dropped)

        with st._lock:
            st.total_chunks = len(all_new)

        BATCH = int(cfg.get("embed_batch_size", DEFAULT_SETTINGS.get("embed_batch_size", 32)))
        for i in range(0, len(all_new), BATCH):
            with st._lock:
                if st.cancel_requested:
                    st.phase = "Cancelled"
                    return
            batch = all_new[i:i + BATCH]
            ids = [chunk_id(c) for c in batch]
            vecs = embed_texts([c["text"] for c in batch], em)
            col.add(
                ids=ids,
                embeddings=vecs,
                documents=[c["text"] for c in batch],
                metadatas=[{
                    "source": c["source"],
                    "page": c["page"],
                    "chunk_index": c.get("chunk_index", 0),
                    "file_hash": c.get("file_hash", ""),
                    # always a string — ChromaDB rejects None metadata values
                    "heading_path": c.get("heading_path", ""),
                } for c in batch],
            )
            for c in batch:
                file_left[c["source"]] -= 1
            with st._lock:
                st.done_chunks = min(i + BATCH, len(all_new))
                for src in {c["source"] for c in batch}:
                    st.file_progress[src] = 100 - round(50 * file_left[src] / file_total[src])

        with st._lock:
            st.done_files   = len(paths)
            st.current_file = ""
            st.phase        = "Building search index..."

        generate_group_overview(group_id, username, paths, col, cfg)

        with st._lock:
            st.phase      = "Generating quick-look summaries..."
            st.done_files = 0
            st.started_at = time.monotonic()   # reset timer for this phase

        def _summarise_progress(name: str, i: int):
            with st._lock:
                st.current_file = name
                st.done_files   = i

        try:
            _card_model = cfg.get("wiki_model") or cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
            _chat_model = cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
            summarize_all_docs(
                group_id, username, paths, col,
                # Structured-JSON task → prefer the stronger wiki/synthesis model.
                model=_card_model,
                force=reset,
                progress_cb=_summarise_progress,
                # Fall back to the chat model for the rare doc whose card JSON the
                # primary model mangles — keeps every doc retrievable (carded).
                fallback_models=[_chat_model],
            )
        except Exception as e:
            log.warning("Doc summaries failed for %s: %s", group_id, e)

        # Build the per-document card index (title/parties/type/focus/keywords)
        # used by document-level retrieval (retrieve_carded). Built from the
        # summaries just generated; embeds one card per document.
        try:
            from local_ai_law.services.doc_cards import build_doc_cards
            with st._lock:
                st.phase = "Building document index..."
            n_cards = build_doc_cards(group_id, username, em)
            log.info("Doc cards built [%s]: %d", group_id, n_cards)
        except Exception as e:
            log.warning("Doc card build failed for %s: %s", group_id, e)

        # Build the frontmatter-derived entity index (party/statute/matter -> docs) used
        # by the cross-matter connection join in retrieve_carded. Deterministic, no LLM —
        # reads the parties/statutes/matter already on the cards. Best-effort: a failure
        # here must never break indexing (the join just falls back to keyword expansion).
        try:
            from local_ai_law.services.entity_index import build_entity_index
            idx = build_entity_index(group_id, username)
            log.info("Entity index built [%s]: %d keys", group_id, len(idx.get("postings") or {}))
        except Exception as e:
            log.warning("Entity index build failed for %s: %s", group_id, e)

        # Build the knowledge graph (off the same index run) when enabled.
        # graph_mode="keyword" (default): instant, deterministic, free — connect
        # documents through their shared card keywords. "llm": legacy per-document
        # relation extraction (slow; falls back relation_model -> wiki_model ->
        # chat_model in wiki.py).
        if cfg.get("auto_graph_build", DEFAULT_SETTINGS.get("auto_graph_build", False)):
            try:
                from local_ai_law.config import load_edition_config
                if load_edition_config().get("graph_enabled", True):
                    with st._lock:
                        st.phase = "Building knowledge graph..."
                        st.done_files = 0
                    if cfg.get("graph_mode", DEFAULT_SETTINGS.get("graph_mode", "keyword")) == "llm":
                        from local_ai_law.services.wiki import build_group_graph
                        n = build_group_graph(username, group_id, paths, cfg, force=reset)
                    else:
                        from local_ai_law.services.wiki import build_keyword_graph
                        n = build_keyword_graph(username, group_id)
                    log.info("Auto graph build [%s]: %d docs", group_id, n)
            except Exception as e:
                log.warning("Auto graph build failed for %s: %s", group_id, e)

        with st._lock:
            st.phase = ""
            st.last_completed = datetime.now().isoformat()
            st.last_indexed_count = st.total_files - st.skipped_files
            st.last_skipped_count = st.skipped_files

    except Exception as e:
        with st._lock:
            st.error = str(e)
            st.phase = ""
        log.error("Index error [%s]: %s", group_id, e, exc_info=True)
    finally:
        with st._lock:
            st.is_indexing = False
