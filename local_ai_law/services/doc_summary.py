"""
Per-document auto summary / meaning extraction.
Generates TL;DR + key points + topics for each uploaded document using local Ollama.
100% private, no cloud calls.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from local_ai_law.config import (
    user_summaries_dir, group_upload_dir, log, atomic_write_json,
    ollama_keep_alive, loads_lenient,
)
from local_ai_law.services.ollama_client import chat as ollama_chat

# Card profile schema version. Bump to force regeneration of summaries that
# predate a card-field change, independent of a full re-index.
CARD_VERSION = 1


def build_card_text(entry: dict) -> str:
    """Compact, identity-rich one-liner per document used for document-level
    retrieval (the "card"). Built from the extracted profile so the search can
    match on title / parties / type, not just body chunks. Generic across any
    document and language — every field is optional and simply omitted if empty.
    """
    if not entry:
        return ""
    parts = []
    title = (entry.get("title") or "").strip()
    if title:
        parts.append(f"Title: {title}")
    dt = (entry.get("doc_type") or "").strip()
    if dt:
        parts.append(f"Type: {dt}")
    parties = entry.get("parties") or []
    if parties:
        parts.append("Parties: " + ", ".join(str(p) for p in parties[:8]))
    if entry.get("privileged"):
        parts.append("Privileged: yes")
    focus = (entry.get("focus") or entry.get("tldr") or "").strip()
    if focus:
        parts.append(f"Focus: {focus}")
    topics = entry.get("topics") or []
    if topics:
        parts.append("Topics: " + ", ".join(str(t) for t in topics[:8]))
    keywords = entry.get("keywords") or []
    if keywords:
        parts.append("Keywords: " + ", ".join(str(k) for k in keywords[:12]))
    return " | ".join(parts)


def _clean_title(t: str) -> str:
    """Strip markdown emphasis/code markers from a title so the stored title
    matches how a user (or the question generator) writes it. Tokenizers drop
    these anyway; removing them keeps the stored title clean and human-faithful."""
    t = re.sub(r"`+", "", t)
    t = t.replace("**", "").replace("*", "")
    return re.sub(r"\s+", " ", t).strip()


def _extract_doc_title(raw: str) -> str:
    """Deterministic, generalizable document title: YAML frontmatter `title:` →
    first markdown `# H1` → "" (caller keeps the LLM-derived title as fallback).

    The LLM paraphrase is lossy — it silently drops disambiguating tokens (e.g. a
    party name in **bold**), so two sibling docs collapse to the same card title.
    The document's own title is what the question (and a user) actually names, so
    prefer it verbatim. Works for any corpus/language; no frontmatter is assumed."""
    if not raw:
        return ""
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if m:
        tm = re.search(r"^title:\s*(.+?)\s*$", m.group(1), re.MULTILINE)
        if tm:
            t = _clean_title(tm.group(1).strip().strip('"').strip("'"))
            if t:
                return t
        body = raw[m.end():]
    else:
        body = raw
    hm = re.search(r"^#\s+(.+?)\s*$", body, re.MULTILINE)
    if hm:
        return _clean_title(hm.group(1))
    return ""


def _frontmatter_fields(raw: str) -> dict:
    """Parse the YAML frontmatter block into a dict (empty if none/unparseable).
    The document's own metadata is authoritative — the LLM extraction is lossy and
    sometimes drops doc_type/parties entirely. Generic: any corpus with frontmatter."""
    if not raw:
        return {}
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if not m:
        return {}
    try:
        import yaml
        d = yaml.safe_load(m.group(1))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _apply_frontmatter(s: dict, raw: str) -> None:
    """Override LLM-extracted card fields with the document's own frontmatter when
    present (title/doc_type/parties/privileged/matter/statutes). In-place; keeps LLM
    values as fallback. Generic across corpora — every field is optional.

    `matter` and `statutes_cited` are the join keys for cross-document retrieval
    (matter-anchored expansion and the entity index); they live only in frontmatter,
    so capture them here or the router has no way to group sibling docs."""
    title = _extract_doc_title(raw)
    if title:
        s["title"] = title
    fm = _frontmatter_fields(raw)
    dt = fm.get("doc_type")
    if isinstance(dt, str) and dt.strip():
        s["doc_type"] = dt.strip()
    parties = fm.get("parties")
    if isinstance(parties, list) and parties:
        s["parties"] = [_clean_title(str(p)) for p in parties if str(p).strip()][:12]
    if "privileged" in fm:
        s["privileged"] = bool(fm.get("privileged"))
    matter = fm.get("matter")
    # YAML reads `matter: 2024.0140` as the float 2024.014: take the text as written.
    if isinstance(matter, (int, float)):
        mm = re.search(r"^matter:\s*['\"]?([^'\"\s]+)", raw, re.MULTILINE)
        matter = mm.group(1) if mm else matter
    if matter is not None and str(matter).strip():
        s["matter"] = str(matter).strip()
    statutes = fm.get("statutes_cited")
    if isinstance(statutes, list) and statutes:
        s["statutes"] = [str(x).strip() for x in statutes if str(x).strip()][:20]


def _summaries_path(group_id: str, username: str = "default") -> Path:
    return user_summaries_dir(username) / f"{group_id}.json"


def load_doc_summaries(group_id: str, username: str = "default") -> dict:
    """Load the group's per-document summary map (doc_name → summary), or {} if absent."""
    p = _summaries_path(group_id, username)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_doc_summaries(group_id: str, summaries: dict, username: str = "default"):
    """Atomically persist the group's per-document summary map."""
    atomic_write_json(_summaries_path(group_id, username), summaries)


def get_doc_summary(group_id: str, doc_name: str, username: str = "default") -> dict | None:
    """Return one document's stored summary dict, or None."""
    return load_doc_summaries(group_id, username).get(doc_name)


def _gather_doc_text(col, doc_name: str, max_chars: int = 6000) -> tuple[str, int]:
    """Pull up to max_chars of text for a single document from the collection."""
    try:
        res = col.get(where={"source": doc_name})
    except Exception:
        return "", 0
    docs = res.get("documents") or []
    if not docs:
        return "", 0
    joined = "\n\n".join(docs)
    total_chunks = len(docs)
    if len(joined) <= max_chars:
        return joined, total_chunks
    third = max_chars // 3
    start = joined[:third]
    mid_start = max(0, (len(joined) - third) // 2)
    middle = joined[mid_start:mid_start + third]
    end = joined[-third:]
    return f"{start}\n\n[...]\n\n{middle}\n\n[...]\n\n{end}", total_chunks


def _repair_truncated_json(s: str) -> str:
    """Best-effort close of a JSON object cut off mid-generation.

    The card model occasionally hits its token budget before emitting the final
    brackets, leaving e.g. `{"a":[1,2` — valid up to the cut, unparseable as-is.
    Walk the text tracking string state and the open-bracket stack, then: close a
    dangling string, drop a trailing comma/colon, and append the missing closers
    in reverse order. Generalises to any doc/language — no corpus assumptions.
    Returns the input unchanged if it already looks balanced.
    """
    stack: list[str] = []
    in_str = False
    esc = False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]" and stack:
            stack.pop()
    if not in_str and not stack:
        return s
    out = s
    if in_str:
        out += '"'
    out = out.rstrip()
    while out and out[-1] in ",:":
        out = out[:-1].rstrip()
    for opener in reversed(stack):
        out += "}" if opener == "{" else "]"
    return out


def summarize_document(
    group_id: str, doc_name: str, col, model: str,
    fallback_models: list[str] | None = None,
) -> dict | None:
    """Build one document card. Tries `model` first, then each fallback in turn.

    A single model occasionally emits JSON we cannot recover (not truncation —
    e.g. qwen3.x deterministically nests `topics`/`keywords` inside another
    array, or splits the card across two objects). Re-asking a *different* model
    fixes these deterministically-bad generations while staying fully local and
    needing no fine-tuning. The indexer passes the chat model as fallback to the
    wiki/card model so a handful of docs don't end up card-less and unretrievable.
    """
    text, total_chunks = _gather_doc_text(col, doc_name)
    if not text:
        log.info("No text for doc summary: %s", doc_name)
        return None
    tried: list[str] = []
    for m in [model, *(fallback_models or [])]:
        if not m or m in tried:
            continue
        tried.append(m)
        s = _summarize_one(doc_name, text, total_chunks, m)
        if s:
            if m != model:
                log.info("Doc summary recovered via fallback model %s: %s", m, doc_name)
            return s
    log.warning("Doc summary JSON unparseable for %s (tried %s)", doc_name, ", ".join(tried))
    return None


def _summarize_one(doc_name: str, text: str, total_chunks: int, model: str) -> dict | None:
    prompt = (
        f'You are analysing a document called "{doc_name}" to help a reader quickly '
        f'understand what it is about, identify it, and decide whether to read it.\n\n'
        f'DOCUMENT CONTENT (may be sampled from start/middle/end):\n{text}\n\n'
        f'Respond with ONLY a JSON object (no markdown fences, no prose) with exactly these fields:\n'
        f'  "title": the document\'s own title or heading verbatim if present, else a short '
        f'descriptive title you derive (one line, no quotes). Keep it in the document\'s language.\n'
        f'  "doc_type": one or two words for the kind of document (e.g. contract, memo, legal '
        f'opinion, letter, pleading, minutes, invoice, report) — in English,\n'
        f'  "parties": array of the named people and organisations involved (verbatim names), '
        f'empty array if none,\n'
        f'  "focus": one sentence stating precisely what this specific document is about,\n'
        f'  "tldr": 2-3 sentence plain-English summary of what this document is about,\n'
        f'  "key_points": array of 3-6 short strings  -  the most important facts, concepts, or arguments,\n'
        f'  "topics": array of 3-6 short topic tag strings (1-3 words each),\n'
        f'  "keywords": array of 5-10 salient terms a reader might search for to find THIS '
        f'document (names, titles, distinctive references/citations), in the document\'s language,\n'
        f'  "what_youll_learn": array of 2-4 short strings framed as "You will understand...", "You will be able to...", etc.\n'
    )

    try:
        resp = ollama_chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            num_ctx=8192,
            # temp 0 → deterministic card extraction (reproducible re-index).
            # num_predict must be generous: the 9-field card for a dense doc can
            # run ~600 tokens, and without a ceiling the model's own default cap
            # truncates the JSON mid-object (unparseable). 1536 clears the card
            # with margin; the repair fallback below catches any rarer overflow.
            options={"temperature": 0.0, "num_predict": 1536},
            keep_alive=ollama_keep_alive(),
        )
        raw = resp["message"]["content"].strip()
        # Reasoning models (e.g. qwen3.x) emit <think>…</think>; strip it so the
        # brace-scan below doesn't span the think block and corrupt the JSON.
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL).strip()

        if raw.startswith("```"):
            lines = raw.splitlines()
            end_idx = len(lines) - 1
            while end_idx > 0 and lines[end_idx].strip() == "```":
                end_idx -= 1
            raw = "\n".join(lines[1:end_idx + 1])

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

        try:
            data = loads_lenient(raw)
        except Exception:
            data = None
        if not isinstance(data, dict):
            # Likely truncated mid-object — try to balance brackets and re-parse
            # rather than throw away the whole card.
            try:
                data = loads_lenient(_repair_truncated_json(raw))
            except Exception:
                data = None
        if not isinstance(data, dict):
            return None  # caller will try the next model / log once
        title = str(data.get("title", "")).strip().strip('"')
        if not title:
            title = Path(doc_name).stem.replace("_", " ").replace("-", " ").strip()
        summary = {
            "doc_name": doc_name,
            "title": title,
            "doc_type": str(data.get("doc_type", "")).strip(),
            "parties": [str(p).strip() for p in (data.get("parties") or []) if p][:12],
            # matter/statutes are frontmatter-only join keys; _apply_frontmatter fills
            # them. Defaulted here so the field always exists (no KeyError downstream).
            "matter": "",
            "statutes": [],
            "focus": str(data.get("focus", "")).strip(),
            "tldr": str(data.get("tldr", "")).strip(),
            "key_points": [str(p).strip() for p in (data.get("key_points") or []) if p][:6],
            "topics": [str(t).strip() for t in (data.get("topics") or []) if t][:6],
            "keywords": [str(k).strip() for k in (data.get("keywords") or []) if k][:12],
            "what_youll_learn": [str(l).strip() for l in (data.get("what_youll_learn") or []) if l][:4],
            "chunks_analysed": total_chunks,
            "card_version": CARD_VERSION,
            "generated_at": datetime.now().isoformat(),
            "model": model,
        }
        return summary

    except json.JSONDecodeError:
        return None  # caller will try the next model / log once
    except Exception as e:
        log.warning("Doc summary failed for %s on %s: %s", doc_name, model, e)
        return None


def summarize_all_docs(
    group_id: str, username: str, paths: list, col,
    model: str, force: bool = False,
    progress_cb=None, fallback_models: list[str] | None = None,
):
    existing = load_doc_summaries(group_id, username)
    updated = dict(existing)
    generated = 0

    for i, path in enumerate(paths):
        try:
            name = path.relative_to(group_upload_dir(group_id, username)).as_posix()
        except Exception:
            name = path.name
        if progress_cb:
            progress_cb(name, i)
        # Reuse the cached card unless forced, missing, or from an older schema.
        cached_ok = (not force and name in existing
                     and existing[name].get("card_version") == CARD_VERSION)
        if cached_ok:
            s = dict(existing[name])
        else:
            log.info("Generating doc summary: %s", name)
            s = summarize_document(group_id, name, col, model, fallback_models)
            if s:
                generated += 1
        if not s:
            continue
        # Override LLM-extracted fields with the document's own frontmatter
        # (title/doc_type/parties/privileged) — deterministic, avoids lossy
        # sibling collapse and thin cards. No-op when there is no frontmatter.
        try:
            _apply_frontmatter(s, path.read_text(errors="ignore"))
        except Exception:
            log.debug("Frontmatter overlay failed for %s; keeping LLM fields", path, exc_info=True)
        if s != existing.get(name):
            # Key strictly by the group-relative path. (Older builds also wrote a
            # basename alias — `updated[path.name] = s` — which collided when two
            # folders held the same filename; the stale-prune below drops those.)
            updated[name] = s
            save_doc_summaries(group_id, updated, username)

    current_names = set()
    for p in paths:
        try:
            current_names.add(p.relative_to(group_upload_dir(group_id, username)).as_posix())
        except Exception:
            current_names.add(p.name)
    for stale in list(updated.keys()):
        if stale not in current_names:
            del updated[stale]

    save_doc_summaries(group_id, updated, username)
    log.info("Doc summaries complete for %s: %d generated, %d total",
             group_id, generated, len(updated))
    return updated
