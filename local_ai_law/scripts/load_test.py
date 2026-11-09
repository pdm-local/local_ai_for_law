#!/usr/bin/env python3
"""Concurrency load test — how the single-GPU box behaves with 1/2/3 lawyers at once.

Drives the REAL serving path (/ws/chat → retrieval → Ollama generation) against an
already-indexed matter, at increasing concurrency. Reports per-request time-to-first-token
(TTFT) and total latency, peak VRAM, and any errors/OOM. The app's GPU gate serialises
generation (one GPU slot), so higher concurrency = queueing, not contention — this quantifies
that queueing so we can promise a realistic per-firm user count.

Run on the box, app up, GPU otherwise idle (respect the one-slot rule — don't run a bench too):
  PYTHONPATH=<repo-parent> venv/bin/python scripts/load_test.py
  GROUP=caa865b5 LEVELS="1 2 3" scripts/load_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics
import subprocess
import threading
import time

import websockets

from local_ai_law.services.auth import create_token
from local_ai_law.config import load_settings

BASE = os.getenv("RAG_BASE", "http://localhost:8000")
USER = os.getenv("RAG_USER", "pdm95")
GROUP = os.getenv("GROUP", "caa865b5")  # nl-realistic bakeoff (459 docs) by default
LEVELS = [int(x) for x in os.getenv("LEVELS", "1 2 3").split()]
TOP_K = int(os.getenv("TOP_K", "8"))

TOKEN = create_token(USER, "user")
MODEL = load_settings(USER).get("chat_model")

# A spread of realistic, corpus-agnostic legal questions (rotated across concurrent clients).
QUERIES = [
    "Wat is het geschil tussen partijen en wie zijn zij?",
    "Welke wetsartikelen worden aangehaald en waarvoor?",
    "Zijn er tegenstrijdigheden tussen de documenten? Vat ze samen.",
    "Wat is de kern van de vordering en het verweer?",
]


def peak_vram_watcher(stop: threading.Event, out: dict):
    peak = 0
    while not stop.is_set():
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=memory.used",
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=5)
            peak = max(peak, int(r.stdout.strip().splitlines()[0]))
        except Exception:
            pass
        time.sleep(0.25)
    out["peak_mib"] = peak


async def ask(idx: int) -> dict:
    q = QUERIES[idx % len(QUERIES)]
    url = f"{BASE.replace('http', 'ws')}/ws/chat?token={TOKEN}"
    t0 = time.time()
    ttft = None
    chars = 0
    err = None
    try:
        async with websockets.connect(url, open_timeout=30, max_size=None,
                                      additional_headers={"Origin": BASE}) as ws:
            await ws.send(json.dumps({"type": "query", "query": q, "group_id": GROUP,
                                      "model": MODEL, "top_k": TOP_K,
                                      "difficulty": "intermediate"}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=600))
                t = m.get("type")
                if t == "chunk":
                    if ttft is None and m.get("content"):
                        ttft = time.time() - t0
                    chars += len(m.get("content", ""))
                elif t == "error":
                    err = m.get("content"); break
                elif t == "done":
                    break
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    return {"total": time.time() - t0, "ttft": ttft, "chars": chars, "err": err}


async def run_level(n: int) -> list[dict]:
    return await asyncio.gather(*(ask(i) for i in range(n)))


def main() -> int:
    print(f"Load test → {BASE}  user={USER} group={GROUP} model={MODEL} top_k={TOP_K}")
    print(f"{'conc':>4} {'ok':>3} {'err':>3} {'TTFT med':>9} {'total med':>10} "
          f"{'total max':>10} {'peakVRAM':>9}")
    overall_ok = True
    for n in LEVELS:
        stop = threading.Event(); vram: dict = {}
        w = threading.Thread(target=peak_vram_watcher, args=(stop, vram), daemon=True); w.start()
        res = asyncio.run(run_level(n))
        stop.set(); w.join(timeout=2)
        oks = [r for r in res if not r["err"]]
        errs = [r for r in res if r["err"]]
        ttfts = [r["ttft"] for r in oks if r["ttft"] is not None]
        totals = [r["total"] for r in oks]
        med_ttft = f"{statistics.median(ttfts):.1f}s" if ttfts else "—"
        med_tot = f"{statistics.median(totals):.1f}s" if totals else "—"
        max_tot = f"{max(totals):.1f}s" if totals else "—"
        print(f"{n:>4} {len(oks):>3} {len(errs):>3} {med_ttft:>9} {med_tot:>10} "
              f"{max_tot:>10} {vram.get('peak_mib','?'):>7} MiB")
        for r in errs:
            overall_ok = False
            print(f"      ! error: {r['err']}")
    print("\nRESULT:", "PASS — no errors/OOM across levels" if overall_ok
          else "FAIL — see errors above")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
