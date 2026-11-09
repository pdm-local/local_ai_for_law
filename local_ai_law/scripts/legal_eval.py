#!/usr/bin/env python3
"""Fixed-question evaluation harness for legal/procurement RAG quality.

Edit scripts/legal_eval_questions.json with your own questions, then run this
after every pipeline change to catch regressions.

Modes:
  python scripts/legal_eval.py            retrieval-only (needs the embed model)
  python scripts/legal_eval.py --full     also generates answers with the chat
                                          model and scores citation quality,
                                          term coverage, category separation,
                                          and honesty

Metrics per question (only expectations present in the JSON are scored):
  correct_source       expected document appears in the retrieved sources
  page_hit             a retrieved chunk falls inside the expected page range
  heading_coverage     share of retrieved chunks carrying a section path
  category_separation  every separate_label is retrieved (and answered) distinctly
  template_match       question-type detector picks the expected template
  citation (--full)    answer cites a document + page for its claims
  completeness (--full) share of must_include_terms present in the answer
  honesty (--full)     answer_absent questions get an explicit "not in the
                       documents" style response
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Grandparent of the package so `local_ai_law.*` imports resolve. Computed, not hardcoded.
_PKG_PARENT = str(Path(__file__).resolve().parents[2])
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

QUESTIONS_FILE = ROOT / "scripts" / "legal_eval_questions.json"

# Citation detection is shared with the live chat post-check so they never drift.
from local_ai_law.services.prompting import has_citation
#: How an answer says the documents do not hold the fact. Both languages, because the
#: answer language follows the user's `app_language` and not the language of the question
#: (see the LANGUAGE RULE in services/prompting.py), so an English question asked against a
#: Dutch user is answered in Dutch. With the English list alone, a model that declined
#: exactly as instructed scored as dishonest, which is a worse lie than the one it grades.
_ABSENT_PHRASES = (
    # English
    "not in the documents", "not found in the documents", "not in the context",
    "not found in context", "no information", "not covered", "not mentioned",
    "does not contain", "do not contain", "could not find", "cannot find",
    "is missing", "not present in",
    # Dutch
    "niet in de documenten", "niet in de context", "vermelden geen", "vermeldt geen",
    "bevatten geen", "bevat geen", "geen informatie", "niet vermeld", "niet genoemd",
    "niet terug te vinden", "niet te vinden", "staat niet in", "ontbreekt",
    "geen specifieke", "niet aangetroffen",
)


def _load_questions() -> list[dict]:
    """Load the editable eval question set from QUESTIONS_FILE."""
    data = json.loads(QUESTIONS_FILE.read_text())
    return data.get("questions", [])


def _retrieve(question: dict, cfg: dict) -> list[dict]:
    """Run the production retrieval pipeline (retrieve_smart) for one question."""
    from local_ai_law.services.indexer import get_group_collection
    from local_ai_law.services.retrieval import retrieve_smart

    username = question.get("username", "default")
    group_id = question.get("group_id", "default")
    col = get_group_collection(group_id, username)
    if col.count() == 0:
        raise RuntimeError(f"no indexed chunks for group '{group_id}' (user '{username}')")
    res = retrieve_smart(
        question["question"], col,
        int(cfg.get("top_k", 10)), cfg["embed_model"],
        username, group_id,
        wiki_top_k=int(cfg.get("wiki_top_k", 3)),
        use_wiki=bool(cfg.get("wiki_first_retrieval", True)),
        settings=cfg,
    )
    return res["results"]


def _answer(question: dict, results: list[dict], cfg: dict) -> str:
    """Generate an answer from the retrieved context (only used with --full)."""
    from local_ai_law.config import load_edition_config
    from local_ai_law.routes.chat import _build_context
    from local_ai_law.services.ollama_client import chat as ollama_chat
    from local_ai_law.services.prompting import build_system_prompt

    context = _build_context(results)
    sysp = build_system_prompt(cfg, load_edition_config(), question["question"])
    user_msg = (
        f"CONTEXT:\n{context}\n\nQUESTION: {question['question']}"
        if context else question["question"]
    )
    resp = ollama_chat(
        cfg["chat_model"],
        [{"role": "system", "content": sysp}, {"role": "user", "content": user_msg}],
        stream=False,
        num_ctx=int(cfg.get("num_ctx", 8192)),
        options={"temperature": 0},
    )
    text = (resp.get("message", {}) or {}).get("content", "") or ""
    return re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.DOTALL).strip()


def _score(question: dict, results: list[dict], answer: str | None) -> dict[str, bool | float | None]:
    """Score one question — retrieval hit (gold doc in results) and, if answered, answer correctness."""
    exp = question.get("expect", {})
    scores: dict[str, bool | float | None] = {}
    chunk_results = [r for r in results if r.get("type") == "chunk"]
    all_text = " ".join(r.get("text", "") for r in results).lower()

    if exp.get("source"):
        want = exp["source"].lower()
        scores["correct_source"] = any(want in str(r.get("source", "")).lower() for r in results)

    if exp.get("page_min") is not None or exp.get("page_max") is not None:
        lo = exp.get("page_min", 1)
        hi = exp.get("page_max", 10**6)
        scores["page_hit"] = any(
            lo <= int(r.get("page") or 0) <= hi for r in chunk_results
        )

    if chunk_results:
        with_heading = sum(1 for r in chunk_results if r.get("heading_path"))
        scores["heading_coverage"] = round(with_heading / len(chunk_results), 2)

    if exp.get("separate_labels"):
        labels = exp["separate_labels"]
        retrieved = all(label.lower() in all_text for label in labels)
        scores["category_separation"] = retrieved
        if answer is not None and retrieved:
            scores["category_separation"] = all(label.lower() in answer.lower() for label in labels)

    if exp.get("type"):
        from local_ai_law.services.answer_templates import detect_question_type
        scores["template_match"] = detect_question_type(question["question"]) == exp["type"]

    if answer is not None:
        if not exp.get("answer_absent"):
            scores["citation"] = has_citation(answer)
        if exp.get("must_include_terms"):
            terms = exp["must_include_terms"]
            found = sum(1 for t in terms if t.lower() in answer.lower())
            scores["completeness"] = round(found / len(terms), 2)
        if exp.get("answer_absent"):
            scores["honesty"] = any(p in answer.lower() for p in _ABSENT_PHRASES)
        if exp.get("type") == "comparison":
            scores["table_format"] = "|" in answer and "---" in answer

    return scores


def main() -> int:
    """CLI entry: run the legal-eval harness (retrieval metrics; +answer scoring with --full)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--full", action="store_true",
                    help="also generate answers with the chat model and score them")
    ap.add_argument("--username", default=None, help="override username for all questions")
    args = ap.parse_args()

    from local_ai_law.config import load_settings

    questions = _load_questions()
    if not questions:
        print("No questions in", QUESTIONS_FILE)
        return 1

    hard_fail = False
    totals: dict[str, list[float]] = {}

    # Say what is actually being measured. The answer language and the system prompt both
    # come from the user the questions name, so a gate run on one machine can score
    # differently from the same gate on another without anything in the code changing.
    first = load_settings(args.username or questions[0].get("username", "default"))
    custom = bool((first.get("system_prompt") or "").strip())
    print(
        f"user={args.username or questions[0].get('username', 'default')} "
        f"answer_language={first.get('app_language') or 'en'} "
        f"chat_model={first.get('chat_model')} "
        f"system_prompt={'customised' if custom else 'edition default'}"
    )

    for q in questions:
        if args.username:
            q["username"] = args.username
        cfg = load_settings(q.get("username", "default"))
        if not cfg.get("embed_model"):
            print("No embed_model configured — finish setup first.")
            return 1
        print(f"\n=== {q.get('id', q['question'][:40])} ===")
        print(f"Q: {q['question']}")
        try:
            results = _retrieve(q, cfg)
        except Exception as e:
            print(f"  RETRIEVAL FAILED: {e}")
            hard_fail = True
            continue

        answer = None
        if args.full:
            try:
                answer = _answer(q, results, cfg)
                preview = answer[:200].replace("\n", " ")
                print(f"A: {preview}{'...' if len(answer) > 200 else ''}")
            except Exception as e:
                print(f"  ANSWER GENERATION FAILED: {e}")

        scores = _score(q, results, answer)
        for metric, value in scores.items():
            mark = value if isinstance(value, float) else ("PASS" if value else "FAIL")
            print(f"  {metric:22} {mark}")
            totals.setdefault(metric, []).append(float(value) if value is not None else 0.0)
            if value is False and metric in ("correct_source", "category_separation"):
                hard_fail = True

    print("\n=== AGGREGATE ===")
    for metric, vals in sorted(totals.items()):
        print(f"  {metric:22} {sum(vals) / len(vals):.0%}  (n={len(vals)})")

    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
