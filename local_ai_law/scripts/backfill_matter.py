#!/usr/bin/env python3
"""Backfill the `matter` (and `statutes`) join keys into already-built doc summaries.

Cards built before the matter field existed have no `matter`/`statutes`, so the
matter-anchored router (services/retrieval.py:_matter_of / retrieve_carded) and the
entity index can't group sibling documents. This is a one-time, **no-LLM, no-GPU**
pass: for every `data/<user>/doc_summaries/<group>.json` it re-reads each document's
YAML frontmatter (authoritative) and writes `matter` + `statutes` into the summary,
falling back to parsing the matter token from the doc NAME when the source file or the
frontmatter field is absent. Idempotent and atomic (reuses save_doc_summaries).

Usage:
    python scripts/backfill_matter.py            # all users, all groups
    python scripts/backfill_matter.py --user U   # one user
    python scripts/backfill_matter.py --dry-run  # report only, write nothing
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from local_ai_law.config import group_upload_dir, BASE_DIR
from local_ai_law.services.doc_summary import (
    load_doc_summaries, save_doc_summaries, _frontmatter_fields,
)
from local_ai_law.services.retrieval import _matter_of


def _enrich(entry: dict, name: str, group_id: str, username: str) -> bool:
    """Fill matter/statutes on one summary entry in place. Returns True if changed."""
    changed = False
    raw = ""
    src = group_upload_dir(group_id, username) / name
    if src.exists():
        try:
            raw = src.read_text(errors="ignore")
        except Exception:
            raw = ""
    fm = _frontmatter_fields(raw) if raw else {}

    # matter: frontmatter first, then name parse (_matter_of handles both).
    if not (entry.get("matter") or "").strip():
        m = str(fm.get("matter") or "").strip() or _matter_of(name, entry)
        if m:
            entry["matter"] = m
            changed = True

    # statutes: frontmatter only (no name signal).
    if not entry.get("statutes"):
        st = fm.get("statutes_cited")
        if isinstance(st, list) and st:
            entry["statutes"] = [str(x).strip() for x in st if str(x).strip()][:20]
            changed = True
    return changed


def _iter_summary_files(only_user: str | None):
    for sf in sorted(BASE_DIR.glob("data/*/doc_summaries/*.json")):
        username = sf.parts[-3]
        group_id = sf.stem
        if only_user and username != only_user:
            continue
        yield username, group_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    total_files = total_docs = total_changed = total_matter = 0
    for username, group_id in _iter_summary_files(args.user):
        summaries = load_doc_summaries(group_id, username)
        if not summaries:
            continue
        total_files += 1
        changed_here = 0
        for name, entry in summaries.items():
            if not isinstance(entry, dict):
                continue
            total_docs += 1
            if _enrich(entry, name, group_id, username):
                changed_here += 1
            if (entry.get("matter") or "").strip():
                total_matter += 1
        total_changed += changed_here
        if changed_here and not args.dry_run:
            save_doc_summaries(group_id, summaries, username)
        print(f"  {username}/{group_id}: {len(summaries)} docs, "
              f"{changed_here} enriched", flush=True)

    verb = "would enrich" if args.dry_run else "enriched"
    print(f"\n{total_files} summary files, {total_docs} docs, "
          f"{verb} {total_changed}; {total_matter} now carry a matter.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
