"""The technical side lives in the owner's .env: a key set there wins over every
settings.json, and /api/settings shows and takes only the user keys from anyone who is
not an admin."""
from __future__ import annotations

import asyncio
import json

import pytest

from local_ai_law import config
from local_ai_law.models import SettingsIn
from local_ai_law.routes import settings as settings_route

ADMIN = {"sub": "owner", "role": "admin"}
LAWYER = {"sub": "lawyer", "role": "user"}


def test_every_tech_key_is_a_setting():
    for key, env in config.TECH_ENV_KEYS:
        assert key in config.DEFAULT_SETTINGS, key
        assert env.startswith("RAG_"), env
    assert not set(config.USER_SETTING_KEYS) & {k for k, _ in config.TECH_ENV_KEYS}


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    glob = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_F", glob)
    monkeypatch.setattr(config, "user_data_dir", lambda u: tmp_path / u)
    (tmp_path / "lawyer").mkdir()
    return tmp_path, glob


def test_env_wins_over_settings_json(dirs, monkeypatch):
    base, glob = dirs
    glob.write_text(json.dumps({"chat_model": "global-pick", "top_k": 4}))
    (base / "lawyer" / "settings.json").write_text(json.dumps({"chat_model": "user-pick", "top_k": 7}))
    monkeypatch.setenv("RAG_CHAT_MODEL", "env-model")
    monkeypatch.setitem(config.DEFAULT_SETTINGS, "chat_model", "env-model")
    monkeypatch.delenv("RAG_TOP_K", raising=False)
    s = config.load_settings("lawyer")
    assert s["chat_model"] == "env-model"
    assert s["top_k"] == 7  # not set in .env, so the user file still counts


def _get(user):
    return asyncio.run(settings_route.api_get_settings(user=user))


def test_get_hides_tech_keys_from_a_user(dirs):
    seen = _get(LAWYER)
    assert set(seen) <= set(config.USER_SETTING_KEYS)
    assert "app_language" in seen
    full = _get(ADMIN)
    assert "chat_model" in full and "top_k" in full


def test_post_from_a_user_changes_only_user_keys(dirs, monkeypatch):
    monkeypatch.setattr(settings_route, "audit_log", lambda *a, **k: None)
    monkeypatch.delenv("RAG_CHAT_MODEL", raising=False)
    body = SettingsIn(app_language="nl", chat_model="sneaky", top_k=3)
    out = asyncio.run(settings_route.api_post_settings(body=body, user=LAWYER))
    assert out == {k: out[k] for k in out if k in config.USER_SETTING_KEYS}
    assert out["app_language"] == "nl"
    stored = config.load_settings("lawyer")
    assert stored["chat_model"] != "sneaky" and stored["top_k"] != 3
