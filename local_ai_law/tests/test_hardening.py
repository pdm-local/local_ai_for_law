"""Security hardening — lockout persistence + pre-setup machine_id loopback gate.

Lockout persistence: brute-force counters must survive an app restart, otherwise an
attacker can reset their attempt budget by triggering a crash/restart.
machine_id: pre-setup the auth middleware is inactive, so the endpoint itself must
refuse non-loopback clients.

Hermetic: temp RAG_DATA_DIR + RAG_USERS_FILE (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from local_ai_law.services import auth as auth_svc
from local_ai_law.routes.setup import machine_id


# ── lockout persistence ───────────────────────────────────────────────────────

def _simulate_restart():
    """Re-load the failure map from disk into a cleared in-memory state, exactly
    what happens when the module is re-imported on app start."""
    auth_svc._fail_log.clear()
    auth_svc._load_fail_log()


def test_lockout_survives_restart():
    user = "bruteforce-victim"
    auth_svc.clear_failed_logins(user)
    for _ in range(auth_svc._MAX_FAILS):
        auth_svc.record_failed_login(user)
    assert auth_svc.is_login_locked(user)
    assert auth_svc._LOCKOUTS_FILE.exists()

    _simulate_restart()
    assert auth_svc.is_login_locked(user), "restart must not clear an active lockout"

    auth_svc.clear_failed_logins(user)
    assert not auth_svc.is_login_locked(user)


def test_clear_removes_persisted_state():
    user = "cleared-user"
    auth_svc.record_failed_login(user)
    auth_svc.clear_failed_logins(user)
    _simulate_restart()
    assert not auth_svc.is_login_locked(user)
    assert not auth_svc._fail_log.get(user)


# ── pre-setup machine_id gate ────────────────────────────────────────────────

def _req(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


def test_machine_id_allows_loopback():
    out = asyncio.run(machine_id(_req("127.0.0.1")))
    assert isinstance(out.get("machine_id"), str) and out["machine_id"]
    out6 = asyncio.run(machine_id(_req("::1")))
    assert out6["machine_id"] == out["machine_id"]


def test_machine_id_rejects_remote():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(machine_id(_req("192.168.1.50")))
    assert exc.value.status_code == 403


def test_machine_id_rejects_missing_client():
    with pytest.raises(HTTPException):
        asyncio.run(machine_id(SimpleNamespace(client=None)))
