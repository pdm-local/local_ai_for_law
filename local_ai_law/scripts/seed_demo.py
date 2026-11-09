#!/usr/bin/env python3
"""Seed the GENERAL full-law demo: two walled lawyer accounts + three indexed hero matters.

The product's live demo. Variant/plan-driven (not firm-specific), so any generated corpus can back
a demo by adding a `CONFIG` entry. Ships the general-practice **Meridiaan Advocaten** corpus
(`legal_corpus/variants/en-fulllaw/`, or `nl-fulllaw`) — three hero matters from *different* practice
areas so the demo shows breadth, each with a planted fact so retrieval visibly lands:

  - Employment (2025.0108): non-compete clause duration + dissolution basis.
  - Workplace injury / tort (2025.0116): DCC tort article + total damages claimed.
  - Shareholders' / corporate (2025.0113): sole director + the dividend distribution-test statute.

Two per-user accounts make the **ethical wall** concrete: logged in as one lawyer you cannot see the
other's dossier. j.havelaar (employment) owns the employment + workplace-injury matters; m.terhorst
(corporate) owns the shareholders' matter. Each account also seeds language-suffixed variants so NL
and EN demos never clobber each other (EN accounts carry an `_en` suffix).

Mechanics: create accounts, one group (matter) per hero
matter, copy that matter's markdown (globbed by the corpus doc-id prefix) into the group's upload
dir, and run a reset index — GPU-serial, one matter at a time (card generation = 1 LLM call per doc;
the reset index also builds the entity index and stores the `matter` key, so matter-anchored
retrieval and the finder work immediately).

Run on the box with the GPU otherwise idle (one-slot rule), from the repository root:

  PYTHONPATH=. \
    local_ai_law/venv/bin/python local_ai_law/scripts/seed_demo.py --variant en-fulllaw

Idempotent-ish: existing users are kept (password reset to the demo password); existing demo groups
with the same name are reused (docs re-copied, re-indexed with reset). Demo password via
RAG_DEMO_PASSWORD (default below). Demo data only — fictional clients. Verify with verify_demo.py.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

from local_ai_law.config import (
    load_groups, save_groups, group_upload_dir, save_settings_file, user_data_dir,
)
from local_ai_law.services.auth import register_user, change_password, _load_users
from local_ai_law.services.indexer import do_index_group, idx_status

DEMO_PW = os.getenv("RAG_DEMO_PASSWORD", "meridiaan-demo-2025")
_VARIANTS = Path(__file__).resolve().parents[2] / "legal_corpus" / "variants"

# Pin the good models so the live demo matches the bench and embeddings stay consistent index↔query
# (qwen3-embedding:4b at index AND query time); app_language drives the UI/finder language.
_SETTINGS = {"chat_model": "gemma4:e4b", "embed_model": "qwen3-embedding:4b", "wiki_model": "qwen3.5:9b"}

# Per-variant demo plan. doc-id prefix is the corpus's firm prefix (fulllaw = "meridiaan-<code>-").
# Each plan entry: (username, [ (group_name, emoji, doc_id_prefix), ... ]).
CONFIG = {
    "en-fulllaw": {
        "corpus": _VARIANTS / "en-fulllaw" / "documents",
        "settings": {**_SETTINGS, "app_language": "en"},
        "desc": "Meridiaan demo (fictional client data)",
        "plan": [
            ("j.havelaar_en", [
                ("Employment — Helix Data (2025.0108)", "\U0001f4bc", "meridiaan-0108-"),
                ("Workplace injury — Orion Media (2025.0116)", "\U0001fa79", "meridiaan-0116-"),
            ]),
            ("m.terhorst_en", [
                ("Shareholders' agreement — Maaslicht (2025.0113)", "\U0001f3db️", "meridiaan-0113-"),
            ]),
        ],
    },
    "nl-fulllaw": {
        "corpus": _VARIANTS / "nl-fulllaw" / "documents",
        "settings": {**_SETTINGS, "app_language": "nl"},
        "desc": "Meridiaan demo (fictieve cliëntdata)",
        "plan": [
            ("j.havelaar", [
                ("Arbeidsrecht — Helix Data (2025.0108)", "\U0001f4bc", "meridiaan-0108-"),
                ("Bedrijfsongeval — Orion Media (2025.0116)", "\U0001fa79", "meridiaan-0116-"),
            ]),
            ("m.terhorst", [
                ("Aandeelhoudersovereenkomst — Maaslicht (2025.0113)", "\U0001f3db️", "meridiaan-0113-"),
            ]),
        ],
    },
}


def ensure_user(username: str, settings: dict) -> None:
    exists = any(u["username"] == username for u in _load_users()["users"])
    if exists:
        change_password(username, DEMO_PW)
        print(f"  user {username}: exists → password reset to demo password")
    else:
        register_user(username, DEMO_PW, role="user")
        print(f"  user {username}: created (role=user)")
    # Pin good models + language for this account (merge onto any existing override).
    sf = user_data_dir(username) / "settings.json"
    cur = json.loads(sf.read_text()) if sf.exists() else {}
    cur.update(settings)
    save_settings_file(cur, username)


def ensure_group(username: str, name: str, emoji: str, language: str, desc: str) -> str:
    groups = load_groups(username)
    for g in groups:
        if g.get("name") == name:
            return g["id"]
    gid = str(uuid.uuid4())[:8]
    groups.append({
        "id": gid, "name": name, "description": desc,
        "color": "#6366f1", "emoji": emoji,
        "language": language,
        "group_type": "knowledge", "created": datetime.now().isoformat(),
    })
    save_groups(groups, username)
    group_upload_dir(username=username, group_id=gid)  # creates the dir
    return gid


def copy_docs(username: str, gid: str, prefix: str, corpus: Path) -> int:
    dest = group_upload_dir(username=username, group_id=gid)
    srcs = sorted(corpus.rglob(f"{prefix}*.md"))
    for s in srcs:
        shutil.copy2(s, dest / s.name)
    return len(srcs)


def main() -> int:
    ap = argparse.ArgumentParser(description="Seed the general full-law demo — walled accounts + indexed hero matters.")
    ap.add_argument("--variant", choices=sorted(CONFIG), default="en-fulllaw",
                    help="corpus variant to seed (default: en-fulllaw). EN seeds the *_en accounts.")
    args = ap.parse_args()
    cfg = CONFIG[args.variant]
    corpus, settings, desc, plan = cfg["corpus"], cfg["settings"], cfg["desc"], cfg["plan"]

    if not corpus.is_dir():
        print(f"ERROR: corpus not found at {corpus}", file=sys.stderr)
        return 1
    language = settings["app_language"]
    print(f"Seeding general full-law demo (variant={args.variant}) from {corpus}")
    print(f"Demo password for all accounts: {DEMO_PW}\n")
    for username, groups in plan:
        print(f"User {username}:")
        ensure_user(username, settings)
        for name, emoji, prefix in groups:
            gid = ensure_group(username, name, emoji, language, desc)
            n = copy_docs(username, gid, prefix, corpus)
            print(f"  group '{name}' [{gid}] ← {n} docs; indexing (reset)…")
            do_index_group(gid, username, True)  # GPU-serial, blocks until done; builds cards + entity index
            st = idx_status(gid, username)
            print(f"    indexed. status: indexing={st.is_indexing} phase='{getattr(st,'phase','')}'")
        print()
    print(f"Done. Two accounts, three hero matters indexed (variant={args.variant}). "
          f"Verify with scripts/verify_demo.py --variant {args.variant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
