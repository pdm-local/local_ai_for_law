"""Refusal-gate regex — must catch genuine "not in the documents" refusals (EN+NL) and
must NOT fire on real grounded answers. A regression here would let fabricated answers
score as refusals (or vice-versa) in the benchmark. Pure regex; no GPU/Ollama.
"""
from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("s", MUST_MATCH)
def test_refusal_matches_genuine_refusals(s):
    assert _REFUSAL.search(s), f"refusal gate failed to match: {s!r}"


@pytest.mark.parametrize("s", MUST_NOT_MATCH)
def test_refusal_does_not_match_real_answers(s):
    assert not _REFUSAL.search(s), f"refusal gate wrongly fired on a grounded answer: {s!r}"
