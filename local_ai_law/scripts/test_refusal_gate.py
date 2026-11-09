#!/usr/bin/env python3
"""Guard against _REFUSAL regex regressions. Correct refusals (EN+NL, the verb
forms the answer model actually emits) MUST match; a fabricated value MUST NOT.
Run: PYTHONPATH=<repo-parent> venv/bin/python scripts/test_refusal_gate.py"""
from __future__ import annotations
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from local_ai_law.scripts.selfheal_bench import _REFUSAL

MUST_MATCH = [
    "The provided context does not contain any information regarding a 2025 merger agreement.",
    "The provided context does not state on what date the dismissal was withdrawn.",
    "There is no such ruling in the documents.",
    "The documents do not mention an agreed purchase price.",
    "De context bevat geen informatie over een fusieovereenkomst uit 2025.",
    "De verstrekte context vermeldt geen uitspraak van een rechter.",
    "Er is geen dergelijke overeenkomst in de stukken.",
    "Dat bedrag is niet vermeld in de documenten.",
]
MUST_NOT_MATCH = [
    "The agreed purchase price was EUR 1,250,000 as stated in the contract.",
    "De overeengekomen koopprijs bedroeg EUR 1.250.000 volgens het contract.",
    "Zenith Pharma B.V. is liable for all damages under section 9.1.",
]

def main() -> int:
    bad = 0
    for s in MUST_MATCH:
        if not _REFUSAL.search(s):
            print(f"FAIL (should match): {s}"); bad += 1
    for s in MUST_NOT_MATCH:
        if _REFUSAL.search(s):
            print(f"FAIL (should NOT match): {s}"); bad += 1
    print("OK" if not bad else f"{bad} failure(s)")
    return 1 if bad else 0

if __name__ == "__main__":
    raise SystemExit(main())
