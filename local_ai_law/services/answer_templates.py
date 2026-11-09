"""Question-type detection and structured answer templates.

Comparison, change-tracking, and obligation questions each need a different
answer shape; injecting the right format into the system prompt prevents
vague summaries and category mixing. Pattern-based detection (same approach
as other low-latency routing helpers) — no LLM call, zero latency.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("comparison", re.compile(
        r"\b(compare|comparison|versus|vs\.?|difference|differences|differ|"
        r"vergelijk|verschil|unterschied|diferencia|différence)\b"
        r"|\bbetween\s+\S.+?\s+and\s+",
        re.IGNORECASE)),
    ("when_to_use", re.compile(
        r"\bwhen\s+(should|to|do(es)?|can|must|is\s+it\s+appropriate)\b.*\buse[d]?\b"
        r"|\binstead\s+of\b|\bin\s+which\s+(cases?|situations?|circumstances)\b",
        re.IGNORECASE | re.DOTALL)),
    ("what_changed", re.compile(
        r"\b(what\s+(has\s+)?changed|change[ds]?|amendment|amended|revision|revised|"
        r"new\s+version|updated?\s+(in|by|since)|gewijzigd|wijziging|änderung)\b",
        re.IGNORECASE)),
    ("obligations", re.compile(
        r"\b(obligation|dut(?:y|ies)|terminat\w+|exit|transition|notice\s+period|"
        r"penalt\w+|liabilit\w+|deadline|must\s+(?:the\s+)?(supplier|contractor|"
        r"authority|buyer|party)|verplichting|opzeg\w+)\b",
        re.IGNORECASE)),
]

_TEMPLATES = {
    "comparison": (
        "Answer as a markdown table: first column = category/clause, one column per "
        "document, contract, or option being compared, one row per category. Never mix "
        "values across columns — every cell comes from exactly one source. Write "
        "'not found in context' for cells the context does not cover. After the table, "
        "add a Sources line listing document and page for each column."
    ),
    "when_to_use": (
        "Structure the answer as:\n"
        "Use it when: (specific conditions from the context, with document and page)\n"
        "Do not use it when: (conditions pointing to alternatives)\n"
        "Alternatives: one line per alternative named in the context, stating exactly "
        "when the context says it applies. Keep every named contract type or category "
        "separate — never blend their criteria."
    ),
    "what_changed": (
        "Structure the answer under three headings: Added, Removed, Modified. Under "
        "Modified show 'old → new' with document and page for both versions. If the "
        "context contains only one version, say that a change comparison is not possible "
        "and describe what that version says instead."
    ),
    "obligations": (
        "Answer as a bullet list grouped per party. For each obligation give: the "
        "obligation itself, deadline or notice period, any condition, the stated "
        "consequence of breach, and the source (document p.N). Quote the exact clause "
        "language for notice periods and penalties. Do not assess enforceability."
    ),
}


def detect_question_type(query: str) -> str | None:
    """First matching type wins; order favours comparison (most specific format)."""
    for qtype, pattern in _PATTERNS:
        if pattern.search(query):
            return qtype
    return None


def get_template(qtype: str) -> str:
    return _TEMPLATES.get(qtype, "")


# Multi-document reasoning detection (compare / connection / contradiction). These
# questions must synthesise across SEVERAL documents, so retrieval needs more breadth
# (more routed docs) and may warrant a stronger synthesis model. Kept separate from
# detect_question_type because connection/contradiction need wider retrieval but no
# special answer *format* (comparison already has its table template above).
_MULTIDOC_PATTERNS: list[re.Pattern] = [
    # A count directly modifying a DOCUMENT noun ("twee documenten", "drie contracten")
    # → the answer spans several docs. Count + ≤2 intervening words + the doc noun, so
    # "twee partijen ... het stuk X" (two parties of ONE doc) does NOT match — counting
    # parties is not counting documents.
    re.compile(
        r"\b(twee|drie|vier|beide|two|three|four|both)\s+([\w-]+\s+){0,2}"
        r"(document\w*|stuk\w*|contract\w*|overeenkomst\w*|memo\w*|brie\w*|"
        r"versie\w*|clausul\w*|clause\w*)\b",
        re.IGNORECASE),
    # Relation / link BETWEEN things → at least two sources.
    re.compile(
        r"\b(verband|relatie|samenhang|connectie|connection|verhouding|link\w*)\b"
        r"[^.?!]{0,40}\b(tussen|between|met\s+elkaar|with\s+each\s+other)\b",
        re.IGNORECASE),
    # Connecting verb joining two named things: "what links X and Y", "verbindt X en Y".
    re.compile(
        r"\b(links?|connects?|relate[sd]?|tie[sd]?\s+together|verbindt|verbinden|"
        r"koppelt?|koppelen)\b[^.?!]{0,50}\b(and|en)\b",
        re.IGNORECASE),
    # Contradiction / inconsistency across documents.
    re.compile(
        r"\b(tegenstrijdig\w*|strijdig\w*|onverenigbaar|inconsisten\w*|"
        r"contradict\w*|conflict\w*|discrepan\w*)\b",
        re.IGNORECASE),
    # Paired ask: "welke X en welke Y" / "which X and which Y".
    re.compile(
        r"\bwelke\b[^.?!]{0,60}\ben\s+welke\b|\bwhich\b[^.?!]{0,60}\band\s+which\b",
        re.IGNORECASE),
]


def is_multidoc(query: str) -> bool:
    """True for questions that must combine SEVERAL documents (compare, connection,
    contradiction). Used to widen retrieval breadth and optionally route to a stronger
    synthesis model. Pattern-based (EN+NL), zero latency. Deliberately conservative:
    single-doc factual/legal-basis/party questions must NOT match, or they'd pay the
    breadth/latency cost for nothing."""
    if detect_question_type(query) == "comparison":
        return True
    return any(p.search(query) for p in _MULTIDOC_PATTERNS)
