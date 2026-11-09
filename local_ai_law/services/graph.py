"""
Local knowledge graph  -  SQLite nodes + edges + FTS5 wiki index.

One DB per user: data/<username>/knowledge.db

Tables:
  nodes       - typed knowledge graph nodes
  edges       - directed labelled relations
  wiki_pages  - wiki page registry (metadata + path)
  wiki_fts    - FTS5 virtual table for wiki search (title/aliases/tags/body)
  doc_states  - per-document processing state (normalize / wiki / graph)

FTS5 fallback: if SQLite was compiled without FTS5, wiki search falls back
to a LIKE query on wiki_pages.title + tags. A startup warning is emitted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from local_ai_law.config import user_data_dir, log

# ── Per-user DB path ──────────────────────────────────────────────────────────

def _db_path(username: str) -> Path:
    return user_data_dir(username) / "knowledge.db"


# ── Thread-local connections ──────────────────────────────────────────────────

_local = threading.local()
# Bumped by reset_connections (a backup restore replaces knowledge.db): a thread whose
# cached connection is older reopens, instead of reading a file that was swapped out.
_generation: dict[str, int] = {}


def reset_connections(username: str) -> None:
    """Make every thread reopen this user's knowledge DB on next use."""
    _generation[username] = _generation.get(username, 0) + 1
    key = f"conn_{username}"
    c = getattr(_local, key, None)
    if c is not None:
        try:
            c.close()
        except Exception:
            log.debug("Closing knowledge DB connection for %s failed", username, exc_info=True)
        setattr(_local, key, None)


def _conn(username: str) -> sqlite3.Connection:
    """Return a thread-local connection to this user's knowledge DB."""
    key = f"conn_{username}"
    gen = _generation.get(username, 0)
    c = getattr(_local, key, None)
    if c is not None and getattr(_local, f"gen_{username}", 0) != gen:
        try:
            c.close()
        except Exception:
            log.debug("Closing stale knowledge DB connection for %s failed", username, exc_info=True)
        c = None
    if c is None:
        path = _db_path(username)
        path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA synchronous=FULL")
        _init_schema(c)
        setattr(_local, key, c)
        setattr(_local, f"gen_{username}", gen)
    return c


# ── FTS5 availability check ───────────────────────────────────────────────────

_fts5_available: Optional[bool] = None
_fts5_lock = threading.Lock()

def _check_fts5(c: sqlite3.Connection) -> bool:
    """Probe whether this SQLite build supports FTS5 (cached module-global; drives the search fallback)."""
    global _fts5_available
    if _fts5_available is not None:
        return _fts5_available
    with _fts5_lock:
        if _fts5_available is not None:
            return _fts5_available
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_test USING fts5(t)")
            c.execute("DROP TABLE IF EXISTS _fts5_test")
            _fts5_available = True
        except sqlite3.OperationalError:
            log.warning(
                "SQLite FTS5 not available — wiki search will use LIKE fallback. "
                "Compile SQLite with FTS5 for best performance."
            )
            _fts5_available = False
    return _fts5_available


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    label       TEXT NOT NULL,
    group_id    TEXT NOT NULL DEFAULT 'default',
    source_path TEXT,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_type    ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_group   ON nodes(group_id);
CREATE INDEX IF NOT EXISTS idx_nodes_label   ON nodes(group_id, label);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation    TEXT NOT NULL,
    weight      REAL NOT NULL DEFAULT 1.0,
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_source   ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target   ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation);

CREATE TABLE IF NOT EXISTS wiki_pages (
    id           TEXT PRIMARY KEY,
    group_id     TEXT NOT NULL,
    page_type    TEXT NOT NULL,
    slug         TEXT NOT NULL,
    title        TEXT NOT NULL,
    aliases      TEXT NOT NULL DEFAULT '[]',
    tags         TEXT NOT NULL DEFAULT '[]',
    source_docs  TEXT NOT NULL DEFAULT '[]',
    related_pages TEXT NOT NULL DEFAULT '[]',
    visibility   TEXT NOT NULL DEFAULT 'internal',
    sensitivity  TEXT NOT NULL DEFAULT 'normal',
    confidence   REAL NOT NULL DEFAULT 0.0,
    file_path    TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(group_id, page_type, slug)
);
CREATE INDEX IF NOT EXISTS idx_wiki_group    ON wiki_pages(group_id);
CREATE INDEX IF NOT EXISTS idx_wiki_type     ON wiki_pages(page_type);
CREATE INDEX IF NOT EXISTS idx_wiki_sensitivity ON wiki_pages(sensitivity);

CREATE TABLE IF NOT EXISTS doc_states (
    id           TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    group_id     TEXT NOT NULL,
    doc_name     TEXT NOT NULL,
    normalize_ok INTEGER NOT NULL DEFAULT 0,
    wiki_ok      INTEGER NOT NULL DEFAULT 0,
    graph_ok     INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL,
    UNIQUE(username, group_id, doc_name)
);
CREATE INDEX IF NOT EXISTS idx_docstate ON doc_states(username, group_id);
"""

_FTS5_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
    page_id UNINDEXED,
    title,
    aliases,
    tags,
    body,
    tokenize='porter ascii'
);
"""


def _init_schema(c: sqlite3.Connection):
    """Create the nodes/edges/wiki_pages/wiki_fts/doc_states tables + indexes if absent (idempotent)."""
    c.executescript(_SCHEMA)
    if _check_fts5(c):
        c.executescript(_FTS5_SCHEMA)
    c.commit()


# ── Node helpers ──────────────────────────────────────────────────────────────

import re as _re

_CONCEPT_SUFFIX = _re.compile(
    r'\s+(model|models|architecture|architectures|mechanism|mechanisms|'
    r'algorithm|algorithms|technique|techniques|method|methods|approach|approaches)$',
    _re.IGNORECASE,
)

_PAREN_ABBREV = _re.compile(r'\s*\([^)]{1,30}\)')  # strip "(RNNs)", "(W, Wk, Wq)" etc.


def normalize_concept(label: str) -> str:
    """Canonical form for node-ID hashing and dedup.
    Strips parentheticals, generic qualifier suffixes, and trailing plurals.
    Examples:
      "Transformer Model"               → "transformer"
      "Recurrent Neural Networks (RNNs)"→ "recurrent neural network"
      "Queries, Keys, and Values"       → "queries, keys, and values"
      "Weight Matrices (W, Wk, Wq)"    → "weight matrice" (close enough for dedup)
      "Self-Attention"                  → "self-attention"
    """
    s = label.strip()
    s = _PAREN_ABBREV.sub('', s).strip()   # remove "(RNNs)" etc.
    s = s.lower()
    # strip trailing generic qualifier word
    stripped = _CONCEPT_SUFFIX.sub('', s).strip()
    if stripped:
        s = stripped
    # For single words: strip trailing plural 's' (>4 chars)
    words = s.split()
    if len(words) == 1 and s.endswith('s') and len(s) > 4:
        s = s[:-1]
    elif len(words) > 1 and words[-1].endswith('s') and len(words[-1]) > 4:
        words[-1] = words[-1][:-1]
        s = ' '.join(words)
    return s


def upsert_node(username: str, node_type: str, label: str, group_id: str = "default",
                source_path: str = "", metadata: dict | None = None) -> str:
    """Insert or update a node. Returns the node id."""
    # Concept nodes: normalized label for stable IDs across variant spellings (we
    # WANT same-concept collapse). Document nodes: key on the source path so two
    # documents with the same title/basename stay distinct (titles are not unique).
    if node_type == "Concept":
        id_label = normalize_concept(label)
    elif node_type == "Document" and source_path:
        id_label = source_path
    else:
        id_label = label
    nid = hashlib.sha256(f"{username}:{group_id}:{node_type}:{id_label}".encode()).hexdigest()[:24]
    c = _conn(username)
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        """INSERT INTO nodes(id, type, label, group_id, source_path, metadata, created_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             label=excluded.label,
             source_path=COALESCE(excluded.source_path, source_path),
             metadata=excluded.metadata""",
        (nid, node_type, label, group_id, source_path,
         json.dumps(metadata or {}), now),
    )
    c.commit()
    return nid


def upsert_edge(username: str, source_id: str, target_id: str,
                relation: str, weight: float = 1.0, metadata: dict | None = None):
    """Insert or update a directed relation edge (id = hash of source:relation:target, so re-adding updates weight/metadata)."""
    eid = hashlib.sha256(f"{source_id}:{relation}:{target_id}".encode()).hexdigest()[:24]
    c = _conn(username)
    now = datetime.now(timezone.utc).isoformat()
    c.execute(
        """INSERT INTO edges(id, source_id, target_id, relation, weight, metadata, created_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET weight=excluded.weight, metadata=excluded.metadata""",
        (eid, source_id, target_id, relation, weight, json.dumps(metadata or {}), now),
    )
    c.commit()


def get_node(username: str, node_id: str) -> dict | None:
    """Fetch one graph node by id, or None."""
    row = _conn(username).execute(
        "SELECT * FROM nodes WHERE id=?", (node_id,)
    ).fetchone()
    return dict(row) if row else None


def expand_node(username: str, node_id: str, depth: int = 1,
                max_nodes: int = 30) -> list[dict]:
    """
    BFS expansion from a node up to `depth` hops.
    Returns list of related node dicts (excludes the root node).
    """
    c = _conn(username)
    visited = {node_id}
    frontier = [node_id]
    results = []

    for _ in range(depth):
        if not frontier or len(results) >= max_nodes:
            break
        placeholders_frontier = ",".join("?" * len(frontier))
        placeholders_visited = ",".join("?" * len(visited))
        rows = c.execute(
            f"""SELECT DISTINCT n.*
                FROM nodes n
                JOIN edges e ON (e.target_id = n.id OR e.source_id = n.id)
                WHERE (e.source_id IN ({placeholders_frontier}) OR e.target_id IN ({placeholders_frontier}))
                  AND n.id NOT IN ({placeholders_visited})
                LIMIT ?""",
            frontier + frontier + list(visited) + [max_nodes - len(results)],
        ).fetchall()
        frontier = []
        for row in rows:
            nd = dict(row)
            if nd["id"] not in visited:
                visited.add(nd["id"])
                frontier.append(nd["id"])
                results.append(nd)

    return results


def find_nodes_by_label(username: str, group_id: str, label: str,
                        node_type: str = "") -> list[dict]:
    """Find up to 20 nodes in a group whose label LIKE-matches `label` (optionally filtered by type)."""
    c = _conn(username)
    if node_type:
        rows = c.execute(
            "SELECT * FROM nodes WHERE group_id=? AND label LIKE ? AND type=? LIMIT 20",
            (group_id, f"%{label}%", node_type),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM nodes WHERE group_id=? AND label LIKE ? LIMIT 20",
            (group_id, f"%{label}%"),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_group_graph(username: str, group_id: str):
    """Delete ONLY the graph nodes and their edges for a group.

    Does NOT touch wiki pages — this runs on every keyword-graph rebuild
    (`build_keyword_graph(clear=True)` after each index), and wiping wiki pages
    here would silently remove them from listing/search on a routine reindex.
    Wiki cleanup on true group delete lives in `delete_group_wiki()`.
    """
    c = _conn(username)
    node_ids = [r[0] for r in c.execute(
        "SELECT id FROM nodes WHERE group_id=?", (group_id,)
    ).fetchall()]
    if node_ids:
        placeholders = ",".join("?" * len(node_ids))
        c.execute(f"DELETE FROM edges WHERE source_id IN ({placeholders})", node_ids)
        c.execute(f"DELETE FROM nodes WHERE id IN ({placeholders})", node_ids)
    c.commit()


def delete_group_wiki(username: str, group_id: str):
    """Delete the wiki-page registry rows and FTS index for a group. Call only on
    actual group deletion — never on graph/keyword-graph rebuild."""
    c = _conn(username)
    wiki_ids = [r[0] for r in c.execute(
        "SELECT rowid FROM wiki_pages WHERE group_id=?", (group_id,)
    ).fetchall()]
    if wiki_ids and _fts5_available:
        placeholders = ",".join("?" * len(wiki_ids))
        c.execute(f"DELETE FROM wiki_fts WHERE rowid IN ({placeholders})", wiki_ids)
    c.execute("DELETE FROM wiki_pages WHERE group_id=?", (group_id,))
    c.commit()


# ── Wiki page registry ────────────────────────────────────────────────────────

def upsert_wiki_page(username: str, page: dict) -> str:
    """
    Register/update a wiki page in the DB and refresh FTS index.
    `page` must include: group_id, page_type, slug, title, file_path.
    Returns page id.
    """
    group_id  = page["group_id"]
    page_type = page["page_type"]
    slug      = page["slug"]
    pid = hashlib.sha256(f"{username}:{group_id}:{page_type}:{slug}".encode()).hexdigest()[:24]
    now = datetime.now(timezone.utc).isoformat()
    c = _conn(username)

    c.execute(
        """INSERT INTO wiki_pages
             (id, group_id, page_type, slug, title, aliases, tags,
              source_docs, related_pages, visibility, sensitivity,
              confidence, file_path, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(group_id, page_type, slug) DO UPDATE SET
             title=excluded.title,
             aliases=excluded.aliases,
             tags=excluded.tags,
             source_docs=excluded.source_docs,
             related_pages=excluded.related_pages,
             visibility=excluded.visibility,
             sensitivity=excluded.sensitivity,
             confidence=excluded.confidence,
             file_path=excluded.file_path,
             updated_at=excluded.updated_at""",
        (
            pid, group_id, page_type, slug,
            page.get("title", slug),
            json.dumps(page.get("aliases", [])),
            json.dumps(page.get("tags", [])),
            json.dumps(page.get("source_docs", [])),
            json.dumps(page.get("related_pages", [])),
            page.get("visibility", "internal"),
            page.get("sensitivity", "normal"),
            float(page.get("confidence", 0.0)),
            page["file_path"],
            now, now,
        ),
    )

    # Sync FTS index
    if _check_fts5(c):
        c.execute("DELETE FROM wiki_fts WHERE page_id=?", (pid,))
        c.execute(
            "INSERT INTO wiki_fts(page_id, title, aliases, tags, body) VALUES(?,?,?,?,?)",
            (
                pid,
                page.get("title", slug),
                " ".join(page.get("aliases", [])),
                " ".join(page.get("tags", [])),
                page.get("body", ""),
            ),
        )

    c.commit()
    return pid


def delete_wiki_page(username: str, page_id: str):
    """Remove a wiki page from the registry and its FTS index row."""
    c = _conn(username)
    c.execute("DELETE FROM wiki_pages WHERE id=?", (page_id,))
    if _check_fts5(c):
        c.execute("DELETE FROM wiki_fts WHERE page_id=?", (page_id,))
    c.commit()


def _parse_wiki_json_fields(d: dict) -> None:
    """Parse JSON-stored array fields on a wiki page dict in-place."""
    import json as _json
    for field in ("aliases", "tags", "source_docs", "related_pages"):
        val = d.get(field)
        if isinstance(val, str):
            try:
                d[field] = _json.loads(val)
            except Exception:
                d[field] = []


def list_wiki_pages(username: str, group_id: str,
                    page_type: str = "", sensitivity_max: str = "sensitive") -> list[dict]:
    """List wiki pages for a group, respecting sensitivity ceiling."""
    _sens_order = {"normal": 0, "internal": 1, "privileged": 2, "sensitive": 3}
    ceiling = _sens_order.get(sensitivity_max, 3)
    c = _conn(username)
    if page_type:
        rows = c.execute(
            "SELECT * FROM wiki_pages WHERE group_id=? AND page_type=? ORDER BY title",
            (group_id, page_type),
        ).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM wiki_pages WHERE group_id=? ORDER BY page_type, title",
            (group_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if _sens_order.get(d.get("sensitivity", "normal"), 0) <= ceiling:
            _parse_wiki_json_fields(d)
            out.append(d)
    return out


def get_wiki_page(username: str, page_id: str) -> dict | None:
    """Fetch one wiki page's registry row (metadata + path) by id, or None."""
    row = _conn(username).execute(
        "SELECT * FROM wiki_pages WHERE id=?", (page_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    _parse_wiki_json_fields(d)
    return d


# ── FTS5 wiki search ──────────────────────────────────────────────────────────

def search_wiki_fts(username: str, group_id: str, query: str,
                    limit: int = 10, sensitivity_max: str = "sensitive") -> list[dict]:
    """
    Search wiki pages using FTS5 (with BM25 ranking).
    Falls back to LIKE if FTS5 is unavailable.
    Returns list of dicts with keys: id, title, page_type, slug, score, snippet.
    """
    _sens_order = {"normal": 0, "internal": 1, "privileged": 2, "sensitive": 3}
    ceiling = _sens_order.get(sensitivity_max, 3)
    c = _conn(username)

    if _check_fts5(c):
        # Sanitize query for FTS5: skip stop-words, use OR so partial matches work
        _STOP = {"tell","me","about","the","a","an","is","are","what","how","why","who",
                 "show","explain","describe","give","find","page","wiki","topic","this","that"}
        sanitized_query = _re.sub(r'[^\w\s-]', ' ', query)
        tokens = [t.strip() for t in sanitized_query.split() if t.strip()]
        content_tokens = [t for t in tokens if t.lower() not in _STOP and len(t) > 1]
        if not content_tokens:
            content_tokens = tokens  # fallback: use all tokens
        safe_q = " OR ".join(f'"{t}"' for t in content_tokens[:8])
        if not safe_q:
            safe_q = sanitized_query[:100]
        try:
            rows = c.execute(
                """SELECT wp.id, wp.title, wp.page_type, wp.slug, wp.sensitivity,
                          wp.confidence, wp.file_path,
                          bm25(wiki_fts) AS score,
                          snippet(wiki_fts, 4, '<b>', '</b>', '...', 20) AS snippet
                   FROM wiki_fts
                   JOIN wiki_pages wp ON wp.id = wiki_fts.page_id
                   WHERE wiki_fts MATCH ? AND wp.group_id = ?
                   ORDER BY score
                   LIMIT ?""",
                (safe_q, group_id, limit * 3),  # fetch extra, then filter by sensitivity
            ).fetchall()
        except sqlite3.OperationalError as e:
            log.warning("FTS5 query error (%s), falling back to LIKE: %s", safe_q, e)
            rows = _like_search(c, group_id, query, limit * 3)
    else:
        rows = _like_search(c, group_id, query, limit * 3)

    results = []
    for row in rows:
        d = dict(row)
        if _sens_order.get(d.get("sensitivity", "normal"), 0) <= ceiling:
            results.append({
                "id":        d["id"],
                "title":     d["title"],
                "page_type": d["page_type"],
                "slug":      d["slug"],
                "file_path": d["file_path"],
                "score":     d.get("score", 0.0),
                "snippet":   d.get("snippet", ""),
                "confidence": d.get("confidence", 0.0),
            })
        if len(results) >= limit:
            break

    return results


def _like_search(c: sqlite3.Connection, group_id: str,
                 query: str, limit: int) -> list[sqlite3.Row]:
    """Fallback wiki search (LIKE over title/tags/aliases) used when SQLite lacks FTS5; fixed 0.5 score."""
    term = f"%{query[:60]}%"
    return c.execute(
        """SELECT id, title, page_type, slug, sensitivity, confidence, file_path,
                  0.5 AS score, '' AS snippet
           FROM wiki_pages
           WHERE group_id=? AND (title LIKE ? OR tags LIKE ? OR aliases LIKE ?)
           ORDER BY title LIMIT ?""",
        (group_id, term, term, term, limit),
    ).fetchall()


# ── Document processing state ─────────────────────────────────────────────────

def get_doc_state(username: str, group_id: str, doc_name: str) -> dict:
    """Return the per-document processing state (normalize/wiki/graph flags) for one doc."""
    row = _conn(username).execute(
        "SELECT * FROM doc_states WHERE username=? AND group_id=? AND doc_name=?",
        (username, group_id, doc_name),
    ).fetchone()
    if row:
        return dict(row)
    return {
        "username": username, "group_id": group_id, "doc_name": doc_name,
        "normalize_ok": 0, "wiki_ok": 0, "graph_ok": 0,
    }


def set_doc_state(username: str, group_id: str, doc_name: str,
                  normalize_ok: bool = None, wiki_ok: bool = None,
                  graph_ok: bool = None):
    """Upsert the normalize/wiki/graph processing flags for one document (None = leave unchanged)."""
    c = _conn(username)
    now = datetime.now(timezone.utc).isoformat()
    existing = get_doc_state(username, group_id, doc_name)
    c.execute(
        """INSERT INTO doc_states
             (id, username, group_id, doc_name, normalize_ok, wiki_ok, graph_ok, updated_at)
           VALUES(?,?,?,?,?,?,?,?)
           ON CONFLICT(username, group_id, doc_name) DO UPDATE SET
             normalize_ok=excluded.normalize_ok,
             wiki_ok=excluded.wiki_ok,
             graph_ok=excluded.graph_ok,
             updated_at=excluded.updated_at""",
        (
            hashlib.sha256(f"{username}:{group_id}:{doc_name}".encode()).hexdigest()[:24],
            username, group_id, doc_name,
            int(normalize_ok) if normalize_ok is not None else existing.get("normalize_ok", 0),
            int(wiki_ok) if wiki_ok is not None else existing.get("wiki_ok", 0),
            int(graph_ok) if graph_ok is not None else existing.get("graph_ok", 0),
            now,
        ),
    )
    c.commit()


def list_doc_states(username: str, group_id: str) -> list[dict]:
    """List the processing state of every document in a group."""
    rows = _conn(username).execute(
        "SELECT * FROM doc_states WHERE username=? AND group_id=? ORDER BY doc_name",
        (username, group_id),
    ).fetchall()
    return [dict(r) for r in rows]
