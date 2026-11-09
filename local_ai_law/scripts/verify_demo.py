#!/usr/bin/env python3
"""Verify the general full-law demo end-to-end over the real /ws/chat path (`--variant`, default en-fulllaw).

For each hero matter it logs in as the owning lawyer, fires the canned demo query, and checks the
answer is grounded on the planted fact (present in the answer or the cited sources). It also runs the
ethical-wall check: with m.terhorst's token, querying j.havelaar's dossier must NOT return that
dossier's content. Targets the accounts seeded by `seed_demo.py --variant <variant>`.

Run with the app up and the GPU idle (one-slot rule), from the repo parent:
  PYTHONPATH=. local_ai_law/venv/bin/python local_ai_law/scripts/verify_demo.py --variant en-fulllaw
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os

import websockets

from local_ai_law.services.auth import create_token
from local_ai_law.config import load_groups, load_settings

BASE = os.getenv("RAG_BASE", "http://localhost:8000")

# Per variant: cases (username, group_name_substring, query, expected_substring_in_sources_or_answer)
# plus the ethical-wall pair (owner_A_user, owner_B_user, B_group_substring, wall_query).
CASES_BY_VARIANT = {
    "en-fulllaw": [
        ("j.havelaar_en", "Employment",
         "How long is the post-termination non-compete clause in the employment agreement?", "12 months"),
        ("j.havelaar_en", "Workplace injury",
         "On what legal basis and for what total amount is Tinexus Media held liable for the workplace accident?", "6:162"),
        ("m.terhorst_en", "Shareholders",
         "Who is the sole director of Maaslicht Cloud and which statute governs their director duties?", "van den Berg"),
    ],
    "nl-fulllaw": [
        ("j.havelaar", "Arbeidsrecht",
         "Hoe lang geldt het concurrentiebeding na afloop van de arbeidsovereenkomst?", "12 maanden"),
        ("j.havelaar", "Bedrijfsongeval",
         "Op welke wettelijke grondslag en voor welk totaalbedrag wordt Tinexus Media aansprakelijk gesteld voor het bedrijfsongeval?", "6:162"),
        ("m.terhorst", "Aandeelhouders",
         "Wie is de enig bestuurder van Maaslicht Cloud en welk wetsartikel regelt zijn bestuurdersplichten?", "van den Berg"),
    ],
}
WALL_BY_VARIANT = {
    "en-fulllaw": ("m.terhorst_en", "j.havelaar_en", "Employment", "What is the non-compete clause in the employment contract?"),
    "nl-fulllaw": ("m.terhorst", "j.havelaar", "Arbeidsrecht", "Wat is het concurrentiebeding in de arbeidsovereenkomst?"),
}


def group_id_for(username: str, name_sub: str) -> str | None:
    for g in load_groups(username):
        if name_sub.lower() in g.get("name", "").lower():
            return g["id"]
    return None


async def ask(token: str, group_id: str, query: str, model: str) -> dict:
    url = f"{BASE.replace('http', 'ws')}/ws/chat?token={token}"
    answer, sources, err = "", [], None
    async with websockets.connect(url, open_timeout=30, max_size=None,
                                  additional_headers={"Origin": BASE}) as ws:
        await ws.send(json.dumps({"type": "query", "query": query, "group_id": group_id,
                                  "model": model, "top_k": 8, "difficulty": "intermediate"}))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
            t = m.get("type")
            if t == "chunk":
                answer += m.get("content", "")
            elif t == "sources":
                sources = m.get("sources") or m.get("content") or []
            elif t == "error":
                err = m.get("content"); break
            elif t == "done":
                break
    return {"answer": answer, "sources": sources, "err": err}


async def run(variant: str) -> int:
    ok = True
    for username, name_sub, query, expect in CASES_BY_VARIANT[variant]:
        token = create_token(username, "user")
        model = load_settings(username).get("chat_model") or "gemma4:e4b"
        gid = group_id_for(username, name_sub)
        if not gid:
            print(f"FAIL  {username}/{name_sub}: group not found"); ok = False; continue
        r = await ask(token, gid, query, model)
        src_blob = json.dumps(r["sources"], ensure_ascii=False)
        hit = (expect in r["answer"]) or (expect in src_blob)
        status = "PASS" if (not r["err"] and r["answer"] and hit) else "WEAK"
        if status != "PASS":
            ok = False
        print(f"[{status}] {username} / {name_sub}")
        print(f"   q: {query}")
        print(f"   sources: {len(r['sources'])}  expect '{expect}' present: {hit}")
        print(f"   answer: {r['answer'][:280].strip()}…\n")

    # Ethical-wall check: m.terhorst's token must not read j.havelaar's employment dossier.
    print("Ethical-wall check:")
    a_user, b_user, b_sub, wall_q = WALL_BY_VARIANT[variant]
    a_token = create_token(a_user, "user")
    b_gid = group_id_for(b_user, b_sub)
    if b_gid:
        r = await ask(a_token, b_gid, wall_q, "gemma4:e4b")
        leaked = bool(r["sources"]) and not r["err"]
        # PASS = no cross-tenant content surfaced (error or empty sources).
        print(f"   {a_user} → {b_user} group {b_gid}: "
              f"sources={len(r['sources'])} err={bool(r['err'])} → "
              f"{'LEAK!' if leaked else 'walled (ok)'}")
        if leaked:
            ok = False
    print("\nRESULT:", "PASS" if ok else "CHECK NEEDED — see above")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Verify the general full-law demo over /ws/chat.")
    ap.add_argument("--variant", choices=sorted(CASES_BY_VARIANT), default="en-fulllaw",
                    help="demo variant to verify (default: en-fulllaw)")
    raise SystemExit(asyncio.run(run(ap.parse_args().variant)))
