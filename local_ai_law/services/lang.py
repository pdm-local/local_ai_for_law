"""Answer language: which chat model answers, and which language a text is in.

The answer language is the group's language, or the app language (`routes/chat.py`). A
firm can run a model trained in one language only (LegalTune's NL and EN models), set as
`chat_model_nl` / `chat_model_en`; a blank one falls back to `chat_model`.

`guess_language` is a stopword count, standard library only. It is for logging an answer
in the wrong language and for the evals, not for routing: the route follows the setting.
"""
from __future__ import annotations

import re

_NL = frozenset(
    "de het een en van in is dat op te zijn voor met niet aan er als bij ook door om "
    "maar nog wordt worden heeft hebben deze dit die naar dan was werd zij hij wij "
    "geen of uit tot over na onder tussen omdat wel zou kan moet".split()
)
_EN = frozenset(
    "the a an and of in is that on to are for with not at there as by also from "
    "but still be been has have this these those then was were she he we no or out "
    "until over after under between because would can must which".split()
)
# Words in both lists ("in", "is", "was", "over") tell nothing apart.
_NL, _EN = _NL - _EN, _EN - _NL
_WORD = re.compile(r"[a-zA-Z\u00c0-\u017f]+")


def chat_model_for(cfg: dict, lang: str) -> str:
    """The chat model for answers in `lang`: `chat_model_<lang>` if set, else `chat_model`."""
    return (cfg.get(f"chat_model_{lang}") or "").strip() or cfg.get("chat_model", "")


def guess_language(text: str, min_hits: int = 8) -> str | None:
    """"nl" or "en" by stopwords, None when the text is too short or too mixed to tell.

    Quoted source text is often in the other language, so it needs a clear lead: the
    winner has at least `min_hits` stopwords and twice the other's count."""
    words = [w.lower() for w in _WORD.findall(text or "")]
    nl = sum(w in _NL for w in words)
    en = sum(w in _EN for w in words)
    if max(nl, en) < min_hits:
        return None
    if nl >= 2 * en:
        return "nl"
    if en >= 2 * nl:
        return "en"
    return None
