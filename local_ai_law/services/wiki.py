"""
Wiki service  -  generate, store, and manage wiki pages.

Each page is a Markdown file with YAML-like frontmatter.
A companion .evidence.json sidecar stores per-section source references.

Page storage: data/<username>/wiki/<group_id>/<page_type>/<slug>.md

Wiki generation uses the configured wiki_model (Ollama).
Generation is best-effort: failures are logged, not fatal.

Sensitivity inheritance:
  - A page inherits the highest sensitivity of its source documents.
  - Admin can explicitly downgrade (e.g., mark a summary as "internal").
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from local_ai_law.config import DEFAULT_SETTINGS, user_data_dir, log, ollama_keep_alive, atomic_write_json, atomic_write_text, safe_num_ctx, loads_lenient
from local_ai_law.services.graph import upsert_wiki_page, upsert_node, upsert_edge
from local_ai_law.services.ollama_client import chat as ollama_chat


# ── Paths ─────────────────────────────────────────────────────────────────────

def wiki_root(username: str, group_id: str) -> Path:
    from local_ai_law.config import validate_safe_id
    validate_safe_id(group_id, "group_id")
    d = user_data_dir(username) / "wiki" / group_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def wiki_page_path(username: str, group_id: str,
                   page_type: str, slug: str) -> Path:
    from local_ai_law.config import validate_safe_id
    validate_safe_id(page_type, "page_type")
    d = wiki_root(username, group_id) / page_type
    d.mkdir(exist_ok=True)
    return d / f"{slug}.md"


def evidence_path(username: str, group_id: str,
                  page_type: str, slug: str) -> Path:
    return wiki_page_path(username, group_id, page_type, slug).with_suffix(".evidence.json")


def _slug(title: str) -> str:
    s = re.sub(r"[^\w\s-]", "", title)
    s = re.sub(r"[\s_]+", "-", s).strip("-").lower()
    return s[:60] or "page"


# ── Frontmatter helpers ───────────────────────────────────────────────────────

def _build_frontmatter(meta: dict) -> str:
    lines = ["---"]
    for k, v in meta.items():
        lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split YAML frontmatter from page body. Returns (meta_dict, body_str)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    fm_block = text[4:end]
    body = text[end + 4:].lstrip("\n")
    meta: dict = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            try:
                meta[k.strip()] = json.loads(v.strip())
            except Exception:
                meta[k.strip()] = v.strip()
    return meta, body


# ── Generation prompts ────────────────────────────────────────────────────────

_WIKI_GEN_PROMPT = """You are a knowledge engineer writing a structured wiki page.

Document excerpt (source: {source}, pages {pages}):
{text}

Write a well-structured wiki page about "{topic}" in the "{page_type}" category.

Requirements:
- Use clear Markdown headings (##, ###).
- Each major section should have 2-4 paragraphs of synthesized content.
- Do NOT copy text verbatim; paraphrase and synthesize.
- Include a ## Summary section at the end.
- Write in {language}.

After each section heading, cite which part of the source supports it in the format:
  <!-- evidence: {source} page N -->

Output only the wiki page body (no frontmatter). Start directly with the first heading."""

_RELATION_PROMPT = """Extract knowledge graph relations from this text.

Text:
{text}

Return a JSON array of relations. Each relation must have:
  {{"source": "<entity label>", "source_type": "<node type>", "relation": "<RELATION>", "target": "<entity label>", "target_type": "<node type>"}}

Valid relation types: MENTIONS, DEFINES, USES, EVALUATES_ON, REPORTS, WRITTEN_BY, CITES,
SUPPORTS, CONTRADICTS, EXTENDS, RELATED_TO, BELONGS_TO, SUMMARIZED_AS, GOVERNED_BY,
DISCUSSED_IN, COMPARES_WITH, REFERENCES, APPLIES_TO

Valid node types: Document, WikiPage, Concept, Topic, Policy, Procedure, Protocol,
Matter, Case, Clause, Paper, Author, Method, Dataset, Meeting, Department, Regulation

Return ONLY valid JSON. No explanations. Max 15 relations."""


# ── Evidence parsing ──────────────────────────────────────────────────────────

def _extract_evidence_from_body(body: str, doc_name: str) -> dict[str, list[dict]]:
    """
    Parse <!-- evidence: <doc> page N --> comments from generated body.
    Returns dict: section_heading → list of evidence refs.
    """
    evidence: dict[str, list[dict]] = {}
    current_section = "(root)"

    for line in body.splitlines():
        m_heading = re.match(r"^#{1,4}\s+(.+)", line)
        if m_heading:
            current_section = m_heading.group(1).strip()
            continue

        m_ev = re.match(
            r"<!--\s*evidence:\s*(.+?)\s+page\s+(\d+)\s*-->", line, re.IGNORECASE
        )
        if m_ev:
            ev_doc = m_ev.group(1).strip()
            ev_page = int(m_ev.group(2))
            if current_section not in evidence:
                evidence[current_section] = []
            evidence[current_section].append({
                "doc": ev_doc,
                "page": ev_page,
                "chunk_id": None,
                "excerpt": "",
            })

    return evidence


def _strip_evidence_comments(body: str) -> str:
    """Remove evidence comments from the displayed wiki body."""
    return re.sub(r"\s*<!--\s*evidence:[^>]*-->\s*", "\n", body).strip()


# ── Core wiki operations ──────────────────────────────────────────────────────

def generate_wiki_page(
    username: str,
    group_id: str,
    page_type: str,
    topic: str,
    normalized_text: str,
    source_docs: list[str],
    settings: dict,
    sensitivity: str = "internal",
    tags: list[str] | None = None,
    language: str = "en",
) -> dict:
    """
    Generate a wiki page for `topic` from normalized text.
    Writes the .md file and .evidence.json sidecar.
    Registers the page in the graph DB.
    Returns a dict with page metadata.
    """
    slug = _slug(topic)
    model = settings.get("wiki_model") or settings.get("chat_model", DEFAULT_SETTINGS["chat_model"])
    num_ctx = safe_num_ctx(settings.get("num_ctx", 8192))
    lang_name = _lang_name(language)

    # Truncate text to fit context window
    max_chars = num_ctx * 3  # rough char → token ratio
    text_input = normalized_text[:max_chars]
    pages_str = _page_range(normalized_text)

    if page_type == "MatterOverview":
        type_instruction = "Act as a concise legal matter overview. Summarize the documents, parties, topics, and key legal or contractual issues covered."
    elif page_type == "Concept":
        type_instruction = "Define and explain this core concept. Focus on its definition, importance, and practical applications."
    else:
        type_instruction = "Provide a general overview and synthesis of the topic."

    prompt = _WIKI_GEN_PROMPT.format(
        source=", ".join(source_docs),
        pages=pages_str,
        text=text_input,
        topic=topic,
        page_type=page_type,
        language=lang_name,
    ) + f"\n\nSpecific Instructions for this page type: {type_instruction}"

    raw_body = None
    last_error = None
    for attempt in range(2):
        try:
            resp = ollama_chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
                num_ctx=num_ctx,
                options={"temperature": 0.3},
                keep_alive=ollama_keep_alive(),
            )
            raw_body = resp["message"]["content"].strip()
            if raw_body:
                break
        except Exception as e:
            last_error = e
            log.warning("Wiki generation attempt %d failed for %s/%s: %s", attempt + 1, page_type, topic, e)

    if not raw_body:
        log.error("Wiki generation completely failed for %s/%s: %s", page_type, topic, last_error)
        raw_body = f"## {topic}\n\n*Wiki generation failed: {last_error}*\n"

    # Extract evidence refs before stripping comments
    evidence_map = _extract_evidence_from_body(raw_body, source_docs[0] if source_docs else "")
    clean_body = _strip_evidence_comments(raw_body)

    now = datetime.now(timezone.utc).isoformat()
    meta = {
        "title": topic,
        "aliases": [],
        "page_type": page_type,
        "edition": settings.get("edition", ""),
        "group": group_id,
        "source_docs": source_docs,
        "related_pages": [],
        "tags": tags or [],
        "created_at": now,
        "updated_at": now,
        "provenance": "auto-generated",
        "visibility": "internal",
        "sensitivity": sensitivity,
        "confidence": 0.75,
        "model": model,
    }

    content = _build_frontmatter(meta) + f"\n\n{clean_body}\n"
    page_path = wiki_page_path(username, group_id, page_type, slug)
    atomic_write_text(page_path, content, encoding="utf-8")

    # Write evidence sidecar
    ev_data = {
        "generated_at": now,
        "model": model,
        "confidence": 0.75,
        "page_sources": source_docs,
        "sections": evidence_map,
    }
    atomic_write_json(evidence_path(username, group_id, page_type, slug), ev_data)

    # Register in graph DB
    page_record = {
        "group_id": group_id,
        "page_type": page_type,
        "slug": slug,
        "title": topic,
        "tags": tags or [],
        "source_docs": source_docs,
        "sensitivity": sensitivity,
        "confidence": 0.75,
        "file_path": str(page_path),
        "body": clean_body[:2000],  # for FTS index
    }
    page_id = upsert_wiki_page(username, page_record)

    # Add node to knowledge graph
    node_id = upsert_node(
        username,
        node_type="WikiPage",
        label=topic,
        group_id=group_id,
        source_path=str(page_path),
        metadata={"page_type": page_type, "slug": slug, "sensitivity": sensitivity},
    )
    # Link wiki page to source docs
    for doc in source_docs:
        doc_node_id = upsert_node(
            username,
            node_type="Document",
            label=doc,
            group_id=group_id,
            source_path=doc,
        )
        upsert_edge(username, doc_node_id, node_id, "SUMMARIZED_AS")

    log.info("Wiki page generated: %s/%s/%s", group_id, page_type, slug)
    return {**page_record, "id": page_id, "node_id": node_id, "path": str(page_path)}


def extract_and_store_relations(
    username: str,
    group_id: str,
    doc_name: str,
    text: str,
    settings: dict,
) -> list[dict]:
    """
    Ask the relation model to extract graph triples from text.
    Stores nodes and edges. Returns list of relation dicts.
    """
    model = settings.get("relation_model") or settings.get("wiki_model") or settings.get("chat_model", DEFAULT_SETTINGS["chat_model"])
    num_ctx = safe_num_ctx(settings.get("num_ctx", 4096), 4096)

    max_chars = 3000
    text_input = text[:max_chars]

    try:
        resp = ollama_chat(
            model=model,
            messages=[{
                "role": "user",
                "content": _RELATION_PROMPT.format(text=text_input),
            }],
            stream=False,
            num_ctx=num_ctx,
            options={"temperature": 0.1},
            keep_alive=ollama_keep_alive(),
        )
        raw = resp["message"]["content"].strip()
        # Strip reasoning-model <think> blocks so the array scan can't catch
        # stray brackets inside the model's reasoning.
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL).strip()

        # Extract JSON array from response (model may add prose)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if not m:
            return []
        relations = loads_lenient(m.group(0))
        if not isinstance(relations, list):
            return []
    except Exception as e:
        log.warning("Relation extraction failed for %s: %s", doc_name, e)
        return []

    valid_relations = {
        "MENTIONS", "DEFINES", "USES", "EVALUATES_ON", "REPORTS", "WRITTEN_BY", "CITES",
        "SUPPORTS", "CONTRADICTS", "EXTENDS", "RELATED_TO", "BELONGS_TO", "SUMMARIZED_AS",
        "GOVERNED_BY", "DISCUSSED_IN", "COMPARES_WITH", "REFERENCES", "APPLIES_TO"
    }

    stored = []
    for rel in relations[:15]:
        try:
            src_id = upsert_node(
                username,
                node_type=rel.get("source_type", "Concept"),
                label=rel["source"],
                group_id=group_id,
                source_path=doc_name,
            )
            tgt_id = upsert_node(
                username,
                node_type=rel.get("target_type", "Concept"),
                label=rel["target"],
                group_id=group_id,
                source_path=doc_name,
            )
            relation_type = rel.get("relation", "RELATED_TO")
            if relation_type not in valid_relations:
                relation_type = "RELATED_TO"
            upsert_edge(username, src_id, tgt_id, relation_type)
            stored.append(rel)
        except Exception as e:
            log.debug("Relation store error: %s — %s", rel, e)

    return stored


def build_keyword_graph(username: str, group_id: str, *,
                        df_cap_frac: float = 0.25,
                        max_keywords_per_doc: int = 10,
                        clear: bool = True) -> int:
    """Build the document graph from shared card keywords — no LLM.

    Each document already has LLM-extracted keywords (in its card). We connect
    every document to its keywords (bipartite Document -> Concept edges), so two
    documents that share a keyword are linked through that keyword node. This is
    deterministic, instant, and free — it replaces the slow per-document LLM
    relation extraction (which also caused the indexing freeze).

    Edge weight = IDF of the keyword: a term shared by few documents is a strong,
    specific link; a term shared by many is weak. Pruning keeps the graph clean:
      * keywords in only ONE document are dropped (they can't connect anything),
      * keywords in more than `df_cap_frac` of documents are dropped (generic
        hubs that would link everything into a hairball),
      * per document we keep the `max_keywords_per_doc` most distinctive (highest
        IDF) connective keywords.

    Returns the number of documents wired into the graph.
    """
    import math
    from collections import Counter
    from local_ai_law.services.doc_summary import load_doc_summaries
    from local_ai_law.services.graph import normalize_concept, delete_group_graph

    summaries = load_doc_summaries(group_id, username)
    if not summaries:
        return 0
    docs = list(summaries.items())
    n = len(docs)

    # Normalize keywords per doc and count document frequency.
    doc_kws: dict[str, set] = {}
    df: Counter = Counter()
    for name, entry in docs:
        kws = set()
        for k in (entry.get("keywords") or []):
            kk = normalize_concept(str(k))
            if kk and len(kk) > 2:
                kws.add(kk)
        doc_kws[name] = kws
        for kk in kws:
            df[kk] += 1

    df_cap = max(2, int(df_cap_frac * n))  # drop keywords more common than this

    if clear:
        delete_group_graph(username, group_id)

    built = 0
    for name, entry in docs:
        # Connective keywords only: shared (df>1) and not a generic hub (df<=cap).
        kept = [(kk, math.log(n / df[kk])) for kk in doc_kws[name]
                if 1 < df[kk] <= df_cap]
        if not kept:
            continue
        kept.sort(key=lambda x: x[1], reverse=True)
        title = entry.get("title") or name
        doc_id = upsert_node(username, node_type="Document", label=title,
                             group_id=group_id, source_path=name,
                             metadata={"doc": name})
        for kk, idf in kept[:max_keywords_per_doc]:
            cid = upsert_node(username, node_type="Concept", label=kk,
                              group_id=group_id)
            upsert_edge(username, doc_id, cid, "HAS_KEYWORD",
                        weight=round(float(idf), 3), metadata={"keyword": kk})
        built += 1
    return built


def build_group_graph(username: str, group_id: str, paths, settings: dict,
                      *, progress=None, force: bool = False) -> int:
    """Extract + store knowledge-graph relations for every document in a group.

    Used by the auto_graph_build trigger (after indexing) and by manual rebuilds.
    Loops the shared single-doc unit `extract_and_store_relations` so the
    extraction logic can never drift between callers. Per-doc text comes from the
    normalizer when present, else a fresh extraction — so it works even when the
    normalize step never ran (e.g. the plain index path). Fail-open per doc.
    Returns the number of documents whose relations were (re)built.
    """
    from local_ai_law.services.normalizer import load_normalized
    from local_ai_law.services.extraction import extract_file
    from local_ai_law.services.graph import get_doc_state, set_doc_state
    from local_ai_law.config import group_upload_dir

    _base = group_upload_dir(group_id, username)
    built = 0
    for path in paths:
        # Relative path is the document identity (doc-state key, normalized-artifact
        # lookup, graph source) — basenames collide across folders.
        try:
            doc_name = path.relative_to(_base).as_posix()
        except Exception:
            doc_name = path.name
        try:
            if not force and get_doc_state(username, group_id, doc_name).get("graph_ok"):
                continue
            text = load_normalized(username, group_id, doc_name)
            if not text:
                pages = extract_file(path)
                text = "\n\n".join(p.get("text", "") for p in pages if p.get("text"))
            if not text or not text.strip():
                continue
            if progress:
                progress(f"Extracting relations for {doc_name}...")
            extract_and_store_relations(username, group_id, doc_name, text[:4000], settings)
            set_doc_state(username, group_id, doc_name, graph_ok=True)
            built += 1
        except Exception as e:
            log.warning("Graph build failed for %s: %s", doc_name, e)
    return built


# ── Page reader (public) ──────────────────────────────────────────────────────

def read_wiki_page(username: str, group_id: str,
                   page_type: str, slug: str) -> dict | None:
    """Read a wiki page and return its metadata + body."""
    path = wiki_page_path(username, group_id, page_type, slug)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)
    ev_path = evidence_path(username, group_id, page_type, slug)
    evidence = {}
    if ev_path.exists():
        try:
            evidence = json.loads(ev_path.read_text(encoding="utf-8"))
        except Exception:
            log.debug("Unreadable evidence sidecar %s; omitting evidence", ev_path, exc_info=True)
    return {
        **meta,
        "body": body,
        "evidence": evidence,
        "slug": slug,
        "page_type": page_type,
        "group_id": group_id,
    }


def read_page_evidence(username: str, group_id: str,
                       page_type: str, slug: str) -> dict:
    ev_path = evidence_path(username, group_id, page_type, slug)
    if not ev_path.exists():
        return {}
    try:
        return json.loads(ev_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def update_wiki_page(username: str, group_id: str, page_type: str,
                     slug: str, new_body: str, meta_updates: dict | None = None):
    """Update the body and/or metadata of an existing page."""
    path = wiki_page_path(username, group_id, page_type, slug)
    if not path.exists():
        raise FileNotFoundError(f"Wiki page not found: {page_type}/{slug}")
    raw = path.read_text(encoding="utf-8")
    meta, _ = _parse_frontmatter(raw)
    meta["updated_at"] = datetime.now(timezone.utc).isoformat()
    if meta_updates:
        meta.update(meta_updates)
    content = _build_frontmatter(meta) + f"\n\n{new_body.strip()}\n"
    atomic_write_text(path, content, encoding="utf-8")
    # Re-index FTS
    upsert_wiki_page(username, {
        **meta,
        "slug": slug,
        "page_type": page_type,
        "file_path": str(path),
        "body": new_body[:2000],
    })


def delete_page_file(username: str, group_id: str, page_type: str, slug: str):
    page_path = wiki_page_path(username, group_id, page_type, slug)
    ev = evidence_path(username, group_id, page_type, slug)
    for p in (page_path, ev):
        if p.exists():
            p.unlink()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lang_name(code: str) -> str:
    return {
        "en": "English", "nl": "Dutch", "ro": "Romanian",
        "de": "German", "es": "Spanish", "fr": "French",
    }.get(code, "English")


def _page_range(text: str) -> str:
    pages = [int(m.group(1)) for m in re.finditer(r"<!--\s*page:(\d+)\s*-->", text)]
    if not pages:
        return "1"
    return f"{min(pages)}-{max(pages)}"
