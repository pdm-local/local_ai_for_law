"""Shared system-prompt assembly.

Used by the chat WebSocket and by scripts/legal_eval.py so the evaluated
prompt can never drift from the one production chat actually sends.
"""

from __future__ import annotations

import re

from local_ai_law.config import (
    DEFAULT_SETTINGS, DIFFICULTY_PROMPTS, LANGUAGE_PROMPTS, DEFAULT_LANGUAGE,
    get_active_edition,
)
from local_ai_law.services.editions import resolve_edition

# A "citation" = a document filename followed shortly by a page reference, e.g.
# "(contract.pdf, p. 3)", "[doc: memo.md | Page: 2]" or "(brief.pdf, pagina 2)". Shared by
# the chat citation post-check (routes/chat.py) and scripts/legal_eval.py so the two
# definitions can never drift apart.
_CITATION_RE = re.compile(
    r"\(?\b\S+\.(pdf|docx|md|txt|xlsx|pptx|html?)\b[^)]{0,30}p(?:age|agina)?[.:]?\s*\d+", re.I)


def has_citation(text: str) -> bool:
    """True if *text* cites a document filename with a page reference (see _CITATION_RE)."""
    return bool(_CITATION_RE.search(text or ""))


def build_system_prompt(
    cfg: dict,
    edition_cfg: dict,
    query: str,
    *,
    group_lang: str = "",
    difficulty: str = "",
    overview: dict | None = None,
) -> str:
    """Assemble the system prompt: base + edition hint + answer template +
    language + difficulty + matter overview. Mirrors chat exactly."""
    active_lang = group_lang or cfg.get("app_language") or DEFAULT_LANGUAGE

    # Select the legal base prompt + answer hint in the active language straight from
    # the edition (source of truth), so existing installs need no edition.json migration.
    ed = resolve_edition(get_active_edition())
    if active_lang == "nl":
        base_default = ed.default_system_prompt_nl or ed.default_system_prompt
        answer_hint  = ed.answer_hint_nl or ed.answer_hint
    else:
        base_default = ed.default_system_prompt
        answer_hint  = ed.answer_hint

    # Respect an explicit user-customised system prompt; otherwise use the language default.
    stored = (cfg.get("system_prompt") or "").strip()
    known_defaults = {
        ed.default_system_prompt, ed.default_system_prompt_nl,
        DEFAULT_SETTINGS.get("system_prompt", ""),
    }
    base_prompt = stored if (stored and stored not in known_defaults) else (base_default or DEFAULT_SETTINGS["system_prompt"])

    if answer_hint:
        base_prompt = f"{base_prompt}\n\n{answer_hint}"

    # Structured answer formats per question type (comparison etc.)
    if cfg.get("answer_templates"):
        from local_ai_law.services.answer_templates import detect_question_type, get_template
        qtype = detect_question_type(query)
        if qtype:
            base_prompt += f"\n\nANSWER FORMAT:\n{get_template(qtype)}"

    lang_prompt = LANGUAGE_PROMPTS.get(active_lang, LANGUAGE_PROMPTS.get(DEFAULT_LANGUAGE, ""))
    sysp = f"{lang_prompt}\n\n{base_prompt}" if lang_prompt else base_prompt

    diff_prompt = DIFFICULTY_PROMPTS.get(difficulty, "")
    if diff_prompt:
        sysp += f"\n\nEXPLANATION STYLE: {diff_prompt}"

    if overview:
        # Keep the per-prompt overview lean: a short matter summary + topics only.
        # The full per-document file list is intentionally omitted — it can be
        # hundreds of lines on large matters and bloats every prompt. Document
        # selection is handled by the retrieval router, and enumeration queries
        # ("list all documents") get the full manifest via meta_doc_context.
        topics = ", ".join(overview.get("topics", [])) or "---"
        summary = (overview.get("summary", "") or "")[:1200]
        sysp += (
            f"\n\nMATTER OVERVIEW:\n{summary}\n"
            f"MAIN TOPICS: {topics}"
        )

    # Grounding / refusal calibration (G-2, 2026-07-07). Scoped to "when context is
    # provided" so it does not clash with the no-context chat branch (which intentionally
    # allows general-knowledge answers). Three parts, each tied to a measured weak spot on
    # the scaled fulllaw bench (NL onbeantwoordbaar 0.141 / tegenstrijdigheid 0.121):
    #   1. Refuse-on-missing-SPECIFIC-fact, in a canonical decline sentence phrased in the
    #      ACTIVE language — the NL answer model was either guessing a value or declining in
    #      wording the scorer's refusal check (_REFUSAL in selfheal_bench.py) does not
    #      recognise, so a correct refusal scored 0. The exemplar below is chosen to match
    #      that regex ("do not contain" / "niet vermeld").
    #   2. Flag CONTRADICTIONS instead of silently picking one value (tegenstrijdigheid).
    #   3. An explicit over-refusal guard so well-supported factual/legal-basis answers are
    #      NOT suppressed — feitelijk/juridisch must not regress.
    decline = (
        "Deze informatie is niet vermeld in de verstrekte documenten."
        if active_lang == "nl" else
        "The provided documents do not contain this information."
    )
    sysp += (
        "\n\nGROUNDING RULE: When document context is provided, answer ONLY from that "
        "context. If a specific detail asked for — a number, amount, date, name, defined "
        "term, or ruling — is not literally present in the provided documents, do NOT "
        "estimate it, infer it, or fill it from general knowledge: say plainly that it is "
        f"not in the documents (e.g. \"{decline}\") and stop. "
        "If two of the provided documents give conflicting information for the same fact, "
        "do NOT silently choose one — state that the documents conflict and cite both. "
        "Equally, when the documents DO support an answer, give it fully and confidently; "
        "only decline when the support is genuinely absent — never refuse a question the "
        "documents actually answer."
    )

    sysp += (
        "\n\nLANGUAGE RULE: Follow the required answer language above even if the user asks "
        "in another language. Keep quoted source text, defined terms, party names, and legal "
        "terms exactly as written in the source; do not translate quotations."
    )

    return sysp
