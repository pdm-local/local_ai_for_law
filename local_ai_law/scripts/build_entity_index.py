#!/usr/bin/env python3
"""Build the frontmatter-derived entity index for existing groups (no LLM, no GPU).

The index (party/statute/matter → docs) is the cross-matter join behind connection
questions; see services/entity_index.py. New indexing builds it automatically inside
do_index_group — this script backfills groups indexed before that hook existed.

Usage:
    python scripts/build_entity_index.py            # all users, all groups
    python scripts/build_entity_index.py --user U   # one user
"""
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from local_ai_law.config import BASE_DIR
from local_ai_law.services.entity_index import build_entity_index


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None)
    args = ap.parse_args()

    files = 0
    for sf in sorted(BASE_DIR.glob("data/*/doc_summaries/*.json")):
        username, group_id = sf.parts[-3], sf.stem
        if args.user and username != args.user:
            continue
        idx = build_entity_index(group_id, username)
        post = idx.get("postings") or {}
        parties = sum(1 for k in post if k.startswith("party:"))
        statutes = sum(1 for k in post if k.startswith("statute:"))
        matters = sum(1 for k in post if k.startswith("matter:"))
        files += 1
        print(f"  {username}/{group_id}: {parties} parties, {statutes} statutes, "
              f"{matters} matters, {len(idx.get('surfaces') or [])} surfaces", flush=True)
    print(f"\nbuilt {files} entity indexes.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
