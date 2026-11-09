"""Auth hardening — token integrity + admin-gated registration.

Registration is the account-provisioning gate for the appliance: the first user bootstraps
(anonymous → admin), and after that only an admin may create accounts. This matches the
ethical-wall model (admin provisions each lawyer's walled account) and stops anyone
reachable on the network from self-registering.

Hermetic: temp RAG_DATA_DIR + RAG_USERS_FILE (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

import jwt
import pytest
from starlette.testclient import TestClient

from local_ai_law.services.auth import (
    create_token, verify_token, JWT_ALGO, register_user, clear_failed_logins,
)
from local_ai_law.app import app

PW = "Str0ng!pass99"


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# ── token integrity ───────────────────────────────────────────────────────────

def test_valid_token_roundtrips():
    info = verify_token(create_token("alice", "user"))
    assert info == {"sub": "alice", "role": "user"}


def test_token_signed_with_wrong_secret_is_rejected():
    forged = jwt.encode({"sub": "alice", "role": "admin"},
                        "attacker-secret-that-is-at-least-32-bytes-long", algorithm=JWT_ALGO)
    assert verify_token(forged) is None


def test_alg_none_token_is_rejected():
    # Classic JWT bypass: an unsigned "alg:none" token must not be accepted.
    unsigned = jwt.encode({"sub": "alice", "role": "admin"}, "", algorithm="none")
    assert verify_token(unsigned) is None


def test_garbage_token_is_rejected():
    assert verify_token("not.a.jwt") is None
    assert verify_token("") is None


# ── admin-gated registration ───────────────────────────────────────────────────

def test_registration_is_admin_gated_after_bootstrap(client):
    # 1) First account: anonymous bootstrap → admin.
    r1 = client.post("/api/auth/register", json={"username": "firmadmin", "password": PW})
    assert r1.status_code == 200, r1.text
    assert r1.json()["user"]["role"] == "admin"
    admin_tok = r1.json()["token"]

    # 2) Anonymous second registration is refused (no open self-registration).
    #    Clear the cookie jar so this request is genuinely unauthenticated — TestClient
    #    otherwise replays firmadmin's auth cookie from step 1.
    client.cookies.clear()
    r2 = client.post("/api/auth/register", json={"username": "intruder", "password": PW})
    assert r2.status_code in (401, 403), r2.text

    # 3) A non-admin cannot provision accounts either.
    #    (firmadmin makes a normal user, then that user tries to register someone.)
    r3 = client.post("/api/auth/register", json={"username": "lawyer2", "password": PW},
                     headers={"Authorization": f"Bearer {admin_tok}"})
    assert r3.status_code == 200, r3.text
    user_tok = create_token("lawyer2", "user")
    r4 = client.post("/api/auth/register", json={"username": "sneak", "password": PW},
                     headers={"Authorization": f"Bearer {user_tok}"})
    assert r4.status_code == 403, r4.text

    # 4) Admin can provision a walled account.
    r5 = client.post("/api/auth/register", json={"username": "lawyer3", "password": PW},
                     headers={"Authorization": f"Bearer {admin_tok}"})
    assert r5.status_code == 200, r5.text
    assert r5.json()["user"]["role"] == "user"


# ── password login + brute-force lockout ────────────────────────────────────────
# Provision via the service layer (register_user) so these don't depend on the
# bootstrap/admin-gate ordering of the registration tests.

def test_login_succeeds_with_correct_password(client):
    register_user("loginok", PW, role="user")
    r = client.post("/api/auth/login", json={"username": "loginok", "password": PW})
    assert r.status_code == 200, r.text
    assert verify_token(r.json()["token"]) == {"sub": "loginok", "role": "user"}


def test_login_wrong_password_is_401(client):
    register_user("loginbad", PW, role="user")
    r = client.post("/api/auth/login", json={"username": "loginbad", "password": "wrong-" + PW})
    assert r.status_code == 401, r.text


def test_login_locks_out_after_repeated_failures(client):
    register_user("lockme", PW, role="user")
    try:
        for _ in range(5):  # _MAX_FAILS (password must clear the 8-char min to reach auth)
            bad = client.post("/api/auth/login", json={"username": "lockme", "password": "wrongpass1"})
            assert bad.status_code == 401, bad.text
        # even the correct password is now rate-limited (429), not accepted.
        locked = client.post("/api/auth/login", json={"username": "lockme", "password": PW})
        assert locked.status_code == 429, locked.text
    finally:
        clear_failed_logins("lockme")  # don't leak lockout state to other tests
