"""Per-user isolation — the ethical wall the law firm relies on.

These tests prove the core invariant: with user A's credentials you cannot reach user B's
chats, sessions, or matters. Isolation here is structural — all data lives under
data/<username>/ and every path helper takes the *authenticated* username, so another
user's group_id resolves to your own (nonexistent) space. The one escape hatch was a
client-supplied session_id used to build a filename; these tests lock that shut.

Hermetic: RAG_DATA_DIR points at a temp tree (see conftest), no Ollama/GPU needed.
"""
from __future__ import annotations

import json

import pytest

from local_ai_law.config import (
    validate_safe_id, user_history_dir, group_upload_dir, get_group,
    save_groups,
)
from local_ai_law.routes.chat import _load_session, _save_session

A = "alice"
B = "bob"


# ── path-traversal primitive ──────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../bob", "..", "a/b", "../../etc/passwd", "foo/../bar", "x\x00y", "a b", "",
])
def test_validate_safe_id_blocks_traversal_and_separators(bad):
    with pytest.raises(ValueError):
        validate_safe_id(bad, "id")


@pytest.mark.parametrize("ok", [
    "alice", "default", "2026-06-25_14-30-00-123456_a1b2c3d4", "group_1.matter-2",
])
def test_validate_safe_id_allows_legit_ids(ok):
    assert validate_safe_id(ok, "id") == ok


# ── chat-history isolation (the explicit ask: no user sees another's chats) ────

def _write_session(username: str, session_id: str, secret: str) -> None:
    d = user_history_dir(username)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{session_id}.json").write_text(json.dumps({
        "session_id": session_id, "started": "now", "turns": [{"user": secret, "assistant": secret}],
    }))


def test_load_session_rejects_traversal_to_other_user():
    # Bob has a confidential session on disk.
    _write_session(B, "bob_secret", "ATTORNEY-CLIENT PRIVILEGED — Bob's matter")
    bob_file = user_history_dir(B) / "bob_secret.json"
    assert bob_file.exists()

    # Alice tries to traverse out of her own history dir into Bob's. Must fail closed.
    for attack in [
        "../../bob/chat_history/bob_secret",
        "..%2f..%2fbob%2fchat_history%2fbob_secret",  # not decoded here, but must still be rejected
        "../bob/chat_history/bob_secret",
    ]:
        assert _load_session(attack, A) is None, f"traversal leaked Bob's session via {attack!r}"


def test_load_session_only_sees_own_dir():
    # Same session_id existing for Bob but not Alice must not resolve for Alice.
    _write_session(B, "shared_id", "Bob private")
    assert _load_session("shared_id", A) is None
    # Positive control: Alice's own session of that id loads (proves validation isn't over-blocking).
    _write_session(A, "shared_id", "Alice own")
    got = _load_session("shared_id", A)
    assert got is not None and got["turns"][0]["user"] == "Alice own"


def test_save_session_rejects_forged_id():
    with pytest.raises(ValueError):
        _save_session({"session_id": "../../bob/chat_history/evil", "turns": []}, A)


# ── matter / group isolation ──────────────────────────────────────────────────

def test_group_lookup_is_per_user():
    # Bob owns a confidential matter; it must be invisible to Alice's group lookup.
    save_groups([{"id": "bob_matter", "name": "Bob v. Aldercrest"}], B)
    save_groups([{"id": "alice_matter", "name": "Alice case"}], A)
    assert get_group("bob_matter", A) is None          # Alice cannot see Bob's matter
    assert get_group("alice_matter", A) is not None     # Alice sees her own


def test_group_upload_dir_blocks_traversal():
    with pytest.raises(ValueError):
        group_upload_dir("../../bob/uploads/secret", A)


# ── legacy single-user groups.json ────────────────────────────────────────────

def test_new_user_does_not_inherit_legacy_groups(tmp_path, monkeypatch):
    """The pre-isolation groups.json holds the first install's matter names. Only the
    admin's one-time migration may take them; any other new user starts clean."""
    from local_ai_law import config
    legacy = tmp_path / "groups.json"
    legacy.write_text(json.dumps([{"id": "leg1", "name": "Client X v. Y", "emoji": "x"}]))
    monkeypatch.setattr(config, "_LEGACY_GROUPS", legacy)

    names = [g["name"] for g in config.load_groups("carol_new")]
    assert "Client X v. Y" not in names

    config.migrate_legacy_data("dave_admin")
    assert "Client X v. Y" in [g["name"] for g in config.load_groups("dave_admin")]
