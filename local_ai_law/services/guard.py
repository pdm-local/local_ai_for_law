"""The front door: every HTTP request and WebSocket comes through here, before auth.

Ported from pdm_chat's guard (2026-10-07), without its Tailscale lookup. Three things are
refused, each of which could let a request past the login or let a web page act as a
signed-in lawyer:

- A path that walks: a `..` or `.` segment, an empty one (`//`), a backslash, a NUL, or a
  slash written as `%2f` / `%5c`. Document routes take a `{rel_path:path}`, so the path is
  never allowed to say anything other than what it plainly says. A path still encoded after
  its one decoding (`%2561pi`) is refused too: anything downstream that decodes again would
  see a different path than the checks did. The frontend encodes a document path one
  segment at a time (`docPathUrl` in index.html), so its own URLs never carry `%2F`.
- A Host that is not this box. On the first run, before any account exists, the API is open
  (the setup wizard). A web page can point a name of its own at the box's address (DNS
  rebinding) and read the answers as its own; refusing an unknown Host stops that.
- A change (POST, PUT, PATCH, DELETE) or a WebSocket opened by a page from another site,
  named by its Origin header. The origin has to be this very address, port and all.

The names that are this box: loopback, any IP address written as such (a rebinding page
always arrives under a name, never under the box's bare IP), the machine's own hostname and
`<hostname>.local`, and every name in `PDM_HOSTS` (comma separated: the LAN name the firm
types, e.g. `law.firm.local`). A box reached by a name not on that list answers 400.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlsplit

_LOOPBACK = {"localhost"}
_CHANGES = {"POST", "PUT", "PATCH", "DELETE"}
_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}
_ENCODED = re.compile(r"%[0-9a-fA-F]{2}")


def _own_names() -> set[str]:
    try:
        name = socket.gethostname().strip().lower()
    except OSError:
        return set()
    return {name, f"{name}.local"} if name else set()


_OWN = _own_names()


def names() -> set[str]:
    """Every name that is this box. `PDM_HOSTS` is read each time, so a test or an admin
    can change it without a restart of this module."""
    extra = {h.strip().lower() for h in os.environ.get("PDM_HOSTS", "").split(",") if h.strip()}
    return _LOOPBACK | _OWN | extra


def _host_only(value: str) -> str:
    """"law.firm.local:8000" -> "law.firm.local", "[::1]:8000" -> "::1"."""
    try:
        return (urlsplit("//" + value).hostname or "").lower()
    except ValueError:
        return ""


def known(host: str) -> bool:
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host in names()


def walks(scope: dict) -> bool:
    """True for a path that is not plainly itself: see the module note."""
    raw = (scope.get("raw_path") or b"").lower()
    if b"%2f" in raw or b"%5c" in raw:
        return True
    path = scope.get("path") or ""
    if "\\" in path or "\x00" in path or _ENCODED.search(path):
        return True
    parts = path.split("/")[1:]
    return any(p in (".", "..") for p in parts) or "" in parts[:-1]


def same_site(origin: str, host_header: str, scheme: str) -> bool:
    """Whether a page of this origin may make changes here: the very address and port this
    request came to. Any other program serving pages on this box has the same name, and its
    pages must not pass as this app's own. "null" is a sandboxed or file page, never us."""
    if origin == "null":
        return False
    try:
        page = urlsplit(origin)
        here = urlsplit(f"{scheme}://{host_header}")
        if page.hostname is None or not known(page.hostname.lower()):
            return False
        mine = _PORTS.get(page.scheme), _PORTS.get(here.scheme)
        return (page.scheme == here.scheme and page.hostname.lower() == (here.hostname or "").lower()
                and (page.port or mine[0]) == (here.port or mine[1]))
    except ValueError:
        return False


def refusal(scope: dict) -> tuple[int, str] | None:
    """(status, message) for a request that may not go on, or None."""
    if walks(scope):
        return 400, "That address is not allowed."
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    host = headers.get("host", "")
    if not known(_host_only(host)):
        return 400, "Unknown host."
    is_ws = scope["type"] == "websocket"
    if is_ws or scope.get("method") in _CHANGES:
        origin = headers.get("origin")
        # A page's own requests name its origin, or nothing at all (curl, scripts, an old
        # browser's same-origin form post). A browser always names it on a WebSocket.
        if origin is not None:
            # Behind nginx the outer scheme is https while this hop is http; the port is
            # what the page saw, which X-Forwarded-Proto tells us.
            scheme = headers.get("x-forwarded-proto") or scope.get("scheme", "http")
            scheme = {"ws": "http", "wss": "https"}.get(scheme, scheme)
            if not same_site(origin, host, scheme):
                return 403, "Refused a change sent from another site."
    return None


class FrontDoor:
    """ASGI middleware, mounted outermost so nothing (auth, routes, static files, the
    WebSocket) ever sees a request it refuses."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        found = refusal(scope)
        if found is None:
            return await self.app(scope, receive, send)
        status, message = found
        if scope["type"] == "websocket":
            # Closing before accept makes the server answer the upgrade with a 403.
            await send({"type": "websocket.close", "code": 4400 if status == 400 else 4403})
            return
        body = json.dumps({"detail": message, "code": "front_door"}).encode()
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]})
        await send({"type": "http.response.body", "body": body})
