"""The front door (services/guard.py): path walks, unknown Hosts and cross-site changes are
refused before auth, for HTTP and WebSocket. Documentation addresses (192.0.2.x) only."""
from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from local_ai_law.services import guard
from local_ai_law.services.guard import FrontDoor


def _app():
    app = FastAPI()

    @app.get("/api/{rest:path}")
    async def read(rest: str):
        return {"rest": rest}

    @app.post("/api/change")
    async def change():
        return {"ok": True}

    @app.websocket("/ws/chat")
    async def chat(ws: WebSocket):
        await ws.accept()
        await ws.send_text("hi")
        await ws.close()

    app.add_middleware(FrontDoor)
    return app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PDM_HOSTS", "law.firm.local")
    return TestClient(_app(), base_url="http://127.0.0.1:8000")


@pytest.mark.parametrize("path", ["/api//b", "/api/a%2fb", "/api/a%2Fb", "/api/a%5cb", "/api/a\\b"])
def test_a_path_that_walks_is_refused(client, path):
    assert client.get(path).status_code == 400


@pytest.mark.parametrize("raw, path", [
    (b"/api/a/../b", "/api/a/../b"), (b"/api/a/./b", "/api/a/./b"),
    (b"/api/a/%2e%2e/b", "/api/a/../b"), (b"/api/%2561pi", "/api/%61pi"),
    (b"/api/a%00b", "/api/a\x00b"),
])
def test_what_an_http_client_would_tidy_is_refused_too(raw, path):
    # httpx tidies dots before sending, so these go to the check as a server would see them.
    assert guard.walks({"raw_path": raw, "path": path})


def test_plain_paths_and_encoded_segments_pass(client):
    assert client.get("/api/groups/g/documents/map/brief%20v2.pdf/file").json() == {
        "rest": "groups/g/documents/map/brief v2.pdf/file"}
    assert client.get("/api/x/").status_code == 200


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost", "[::1]:8000", "192.0.2.10:8000",
                                  "law.firm.local", "LAW.FIRM.LOCAL:443"])
def test_known_hosts_pass(client, host):
    assert client.get("/api/x", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.test", "law.firm.local.evil.test", "", "testserver"])
def test_an_unknown_host_is_refused(client, host):
    r = client.get("/api/x", headers={"host": host})
    assert r.status_code == 400
    assert r.json()["code"] == "front_door"
    assert r.headers["cache-control"] == "no-store"


def test_pdm_hosts_is_read_each_time(client, monkeypatch):
    assert client.get("/api/x", headers={"host": "box.lan"}).status_code == 400
    monkeypatch.setenv("PDM_HOSTS", "box.lan, other.lan")
    assert client.get("/api/x", headers={"host": "box.lan"}).status_code == 200


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", None])
def test_a_change_from_this_app_or_no_page_passes(client, origin):
    headers = {"origin": origin} if origin else {}
    assert client.post("/api/change", headers=headers).status_code == 200


@pytest.mark.parametrize("origin", ["http://evil.test", "null", "http://127.0.0.1:3000",
                                    "https://127.0.0.1:8000", "http://192.0.2.10:8000", "garbage"])
def test_a_change_from_another_site_is_refused(client, origin):
    assert client.post("/api/change", headers={"origin": origin}).status_code == 403


def test_a_read_from_another_site_is_left_to_cors(client):
    assert client.get("/api/x", headers={"origin": "http://evil.test"}).status_code == 200


def test_behind_nginx_the_forwarded_scheme_counts(client):
    headers = {"host": "law.firm.local", "origin": "https://law.firm.local", "x-forwarded-proto": "https"}
    assert client.post("/api/change", headers=headers).status_code == 200


def test_websocket_from_this_app_opens(client):
    with client.websocket_connect("ws://127.0.0.1:8000/ws/chat",
                                 headers={"origin": "http://127.0.0.1:8000"}) as ws:
        assert ws.receive_text() == "hi"


@pytest.mark.parametrize("headers", [{"origin": "http://evil.test"}, {"host": "evil.test"}])
def test_websocket_from_elsewhere_is_refused(client, headers):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1:8000/ws/chat", headers=headers) as ws:
            ws.receive_text()


def test_the_real_app_mounts_the_front_door_first():
    from local_ai_law.app import app
    assert app.user_middleware[0].cls is guard.FrontDoor
