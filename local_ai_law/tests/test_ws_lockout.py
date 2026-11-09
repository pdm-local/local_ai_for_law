"""P0-2 regression: the /ws/chat WebSocket must enforce the instance lockout.

The HTTP auth/licensing middleware skips WebSocket upgrade requests, so the lockout + license
gate has to be enforced inside the handler *before* `ws.accept()`. Otherwise a locked (or
unlicensed) instance would still serve chat over the socket. A valid token is supplied so the
test reaches the lockout check regardless of how many users exist in the shared test store.

Hermetic: temp RAG_DATA_DIR (see conftest); no Ollama/GPU needed.
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from local_ai_law.app import app
from local_ai_law.services.auth import create_token

LOCK_CLOSE_CODE = 4423  # matches routes/chat.py


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def test_ws_chat_refused_when_instance_locked(client, monkeypatch):
    # The handler does `from ...services.license import is_locked` at call time, so patching
    # the module attribute is what the handler will see.
    monkeypatch.setattr("local_ai_law.services.license.is_locked", lambda: True)
    tok = create_token("tester", "admin")

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws/chat?token={tok}") as ws:
            ws.receive_text()  # server closed before accept; disconnect surfaces here or on enter
    assert excinfo.value.code == LOCK_CLOSE_CODE, \
        "REGRESSION (P0-2): locked instance did not refuse the /ws/chat handshake"


def test_ws_chat_accepts_when_unlocked(client, monkeypatch):
    # Positive control: with the instance unlocked, the handshake must be accepted (no pre-accept
    # close). Entering the context without a WebSocketDisconnect proves ws.accept() ran.
    monkeypatch.setattr("local_ai_law.services.license.is_locked", lambda: False)
    tok = create_token("tester", "admin")

    with client.websocket_connect(f"/ws/chat?token={tok}"):
        pass  # accepted, then cleanly closed by the context manager
