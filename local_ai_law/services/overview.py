"""Matter overview generation: summary, topics, per-document key ideas, and wiki page."""

import json
import re
from datetime import datetime
from pathlib import Path

from local_ai_law.config import (
    user_overviews_dir, DEFAULT_SETTINGS, get_group, group_upload_dir, log,
    ollama_keep_alive, atomic_write_json, atomic_write_text, loads_lenient,
)
from local_ai_law.services.ollama_client import chat as ollama_chat


def overview_path(group_id: str, username: str = "default") -> Path:
    """Path to a group's saved overview JSON."""
    return user_overviews_dir(username) / f"{group_id}.json"


def load_overview(group_id: str, username: str = "default") -> dict | None:
    """Load a group's saved overview JSON, or None if absent/unreadable."""
    p = overview_path(group_id, username)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return None
    return None


def generate_group_overview(group_id: str, username: str, paths: list[Path], col, cfg: dict):
    """Generate overview + per-document key ideas, save JSON, and write a wiki page."""
    # Structured-JSON task → prefer the (stronger) wiki/synthesis model when set;
    # small chat models often emit invalid JSON for these.
    model = cfg.get("wiki_model") or cfg.get("chat_model", DEFAULT_SETTINGS["chat_model"])
    group = get_group(group_id, username)
    gname = group["name"] if group else group_id
    gdesc = group.get("description", "") if group else ""

    if not paths:
        log.info("No documents for overview in group %s", group_id)
        _save_fallback(group_id, username, model, gname, paths)
        return

    doc_samples: list[str] = []
    upload_base = group_upload_dir(group_id, username)
    for path in paths:
        try:
            source_name = path.relative_to(upload_base).as_posix()
        except Exception:
            source_name = path.name
        try:
            res = col.get(where={"source": source_name})
            docs = (res.get("documents") or [])[:3]
            sample = " ... ".join(docs)[:600]
        except Exception:
            sample = ""
        doc_samples.append(f'"{source_name}": {sample}')

    doc_block = "\n".join(doc_samples)
    desc_part = f' described as: "{gdesc}"' if gdesc else ""

    prompt = (
        f'You are cataloguing a legal matter workspace called "{gname}"{desc_part}.\n'
        f'Below are the files with sample text from each:\n\n{doc_block}\n\n'
        'Respond with ONLY a JSON object (no markdown fences) with these fields:\n'
        '  "summary"   : 2-3 sentence overview of what this matter workspace covers\n'
        '  "topics"    : array of 3-8 main topic strings\n'
        '  "documents" : array of objects with fields:\n'
        '      "name"       : the document name EXACTLY as written above (keep any folder/ prefix)\n'
        '      "description": one sentence describing this document\n'
        '      "key_ideas"  : array of exactly 3-5 short bullet-point strings (key concepts/takeaways)\n'
    )

    try:
        resp = ollama_chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            num_ctx=8192,
            options={"temperature": 0},
            keep_alive=ollama_keep_alive(),
        )
        raw = resp["message"]["content"].strip()
        # Strip reasoning-model <think> blocks before the brace-scan.
        raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL).strip()
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

        data = loads_lenient(raw)
        if not isinstance(data, dict):
            raise ValueError("overview JSON unparseable")
        overview = {
            "group_id":     group_id,
            "generated_at": datetime.now().isoformat(),
            "model":        model,
            "summary":      data.get("summary", ""),
            "topics":       data.get("topics", []),
            "documents":    data.get("documents", []),
        }
        atomic_write_json(overview_path(group_id, username), overview)
        log.info("Overview generated for group %s", group_id)
        _write_matter_overview_wiki(group_id, username, overview)

    except json.JSONDecodeError as e:
        log.warning("Overview JSON parse failed for %s: %s — saving fallback", group_id, e)
        _save_fallback(group_id, username, model, gname, paths)
    except Exception as e:
        log.warning("Overview generation failed for %s: %s — saving fallback", group_id, e)
        _save_fallback(group_id, username, model, gname, paths)


def _write_matter_overview_wiki(group_id: str, username: str, overview: dict):
    """Write/overwrite a MatterOverview wiki page aggregating matter information."""
    try:
        from datetime import timezone
        from local_ai_law.services.wiki import wiki_page_path, _slug, _build_frontmatter
        from local_ai_law.services.graph import list_wiki_pages, upsert_wiki_page

        group = get_group(group_id, username)
        gname = (group or {}).get("name", group_id)
        docs = overview.get("documents", [])
        topics = overview.get("topics", [])
        summary = overview.get("summary", "")
        page_type = "MatterOverview"
        slug = _slug(f"matter-overview-{group_id}")
        title = f"Matter Overview - {gname}"
        now = datetime.now(timezone.utc).isoformat()

        # ── markdown body ─────────────────────────────────────────────────────
        lines = [f"## Matter Overview\n\n{summary}\n"]

        if topics:
            lines.append("## Main Topics\n")
            for t in topics:
                lines.append(f"- {t}")
            lines.append("")

        lines.append(f"## Documents ({len(docs)} files)\n")
        for d in docs:
            lines.append(f"### {d['name']}\n")
            if d.get("description"):
                lines.append(f"{d['description']}\n")
            ideas = d.get("key_ideas", [])
            if ideas:
                lines.append("**Key ideas:**\n")
                for idea in ideas:
                    lines.append(f"- {idea}")
            lines.append("")

        wiki_pages = list_wiki_pages(username, group_id)
        # Exclude the matter overview page itself to avoid self-reference.
        wiki_pages = [p for p in wiki_pages if p.get("page_type") != page_type]
        if wiki_pages:
            lines.append(f"## Wiki Pages ({len(wiki_pages)} pages)\n")
            by_type: dict[str, list] = {}
            for p in wiki_pages:
                by_type.setdefault(p.get("page_type", "page"), []).append(p)
            for ptype, pages in sorted(by_type.items()):
                lines.append(f"### {ptype.replace('_', ' ').title()}\n")
                for p in sorted(pages, key=lambda x: x.get("title", "")):
                    lines.append(f"- {p['title']}")
                lines.append("")

        body = "\n".join(lines).strip()

        meta = {
            "title":         title,
            "group":         group_id,
            "group_id":      group_id,
            "page_type":     page_type,
            "slug":          slug,
            "aliases":       [gname, "matter overview", "overview"],
            "tags":          ["matter-overview", "overview"],
            "source_docs":   ["[auto-generated]"],
            "related_pages": [],
            "visibility":    "internal",
            "sensitivity":   "normal",
            "confidence":    1.0,
            "provenance":    "auto-generated",
            "created_at":    now,
            "updated_at":    now,
        }
        path = wiki_page_path(username, group_id, page_type, slug)
        atomic_write_text(path, _build_frontmatter(meta) + f"\n\n{body}\n", encoding="utf-8")

        upsert_wiki_page(username, {
            **meta,
            "file_path": str(path),
            "body": body[:2000],
        })
        log.info("Matter overview wiki page written for %s/%s", username, group_id)
    except Exception as e:
        log.warning("Could not write matter overview wiki page for %s: %s", group_id, e)


def _save_fallback(group_id: str, username: str, model: str, gname: str, paths: list[Path]):
    upload_base = group_upload_dir(group_id, username)

    def _rel(p: Path) -> str:
        # Same group-relative name the model path uses, so nested same-named docs
        # (folder_a/report.md vs folder_b/report.md) stay distinct in the fallback.
        try:
            return p.relative_to(upload_base).as_posix()
        except Exception:
            return p.name

    overview = {
        "group_id":     group_id,
        "generated_at": datetime.now().isoformat(),
        "model":        model,
        "summary":      f'Matter "{gname}" - {len(paths)} document(s).',
        "topics":       [],
        "documents":    [{"name": _rel(p), "description": "", "key_ideas": []} for p in paths],
    }
    atomic_write_json(overview_path(group_id, username), overview)
    log.info("Fallback overview saved for group %s", group_id)
