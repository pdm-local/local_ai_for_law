"""The answer language picks the chat model; guess_language tells Dutch from English."""
from __future__ import annotations

from local_ai_law.services.lang import chat_model_for, guess_language


def test_language_model_when_set():
    cfg = {"chat_model": "base", "chat_model_nl": "lt-nl-v1", "chat_model_en": ""}
    assert chat_model_for(cfg, "nl") == "lt-nl-v1"
    assert chat_model_for(cfg, "en") == "base"


def test_falls_back_to_chat_model():
    assert chat_model_for({"chat_model": "base"}, "nl") == "base"
    assert chat_model_for({"chat_model": "base", "chat_model_nl": "  "}, "nl") == "base"


def test_guess_dutch_and_english():
    nl = ("De huurder is op grond van de overeenkomst niet verplicht om de kosten te dragen, "
          "omdat de verhuurder dit in de brief van 3 maart heeft erkend en er geen andere "
          "afspraak is gemaakt.")
    en = ("The tenant is not obliged under the agreement to bear the costs, because the "
          "landlord has accepted this in the letter of 3 March and there was no other "
          "arrangement between the parties.")
    assert guess_language(nl) == "nl"
    assert guess_language(en) == "en"


def test_short_or_mixed_text_is_unknown():
    assert guess_language("Art. 6:248 BW") is None
    assert guess_language("") is None
