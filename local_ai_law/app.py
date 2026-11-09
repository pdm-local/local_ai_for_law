#!/usr/bin/env python3
"""
pdm Legal Workspace  -  local-first document intelligence platform.
100% private: Ollama only, no cloud APIs.

Run (dev):   uvicorn app:app --host 0.0.0.0 --port 8000 --reload
Run (prod):  gunicorn -c gunicorn.conf.py app:app
"""

from contextlib import asynccontextmanager

# Put the package's parent dir on sys.path so `local_ai_law.*` imports
# resolve regardless of launcher (uvicorn/gunicorn/run.sh). Computed, not hardcoded.
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from local_ai_law.config import (
    BASE_DIR, HOST, PORT, HTTPS_ENABLED, CERT_FILE, KEY_FILE,
    get_branding, setup_logging, ensure_dirs, check_ollama, log,
    is_setup_complete,
)

# ── Setup ─────────────────────────────────────────────────────────────────────
setup_logging()
ensure_dirs()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ───────────────────────────────────────────────────────────────
    log.info("%s starting on %s:%s", get_branding()["app_name"], HOST, PORT)

    if check_ollama():
        log.info("Ollama connection OK")
        # Validate that the configured chat + embed models are actually installed,
        # so a missing model surfaces at startup instead of mid-request.
        try:
            from local_ai_law.config import load_settings
            from local_ai_law.services.ollama_client import list_models
            installed = set(list_models())
            cfg = load_settings("default")
            for role in ("chat_model", "embed_model"):
                name = (cfg.get(role) or "").strip()
                if name and name not in installed:
                    log.warning("Configured %s '%s' is not installed in Ollama "
                                "(run: ollama pull %s)", role, name, name)
        except Exception as e:
            log.debug("Startup model validation skipped: %s", e)
    else:
        log.warning("Ollama not reachable — chat/indexing will fail until it starts")

    if not is_setup_complete():
        log.info("First launch detected — setup wizard will appear on browser load")
    else:
        from local_ai_law.config import get_active_edition
        log.info("Edition: %s", get_active_edition())

    from local_ai_law.services.backup import start_backup_scheduler
    start_backup_scheduler()

    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    log.info("%s shutting down gracefully", get_branding()["app_name"])


app = FastAPI(title=get_branding()["app_name"], lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

# ── Register route modules ────────────────────────────────────────────────────
from local_ai_law.routes.auth      import router as auth_router
from local_ai_law.routes.meta      import router as meta_router
from local_ai_law.routes.settings  import router as settings_router
from local_ai_law.routes.groups    import router as groups_router
from local_ai_law.routes.documents import router as documents_router
from local_ai_law.routes.indexing  import router as indexing_router
from local_ai_law.routes.sessions  import router as sessions_router
from local_ai_law.routes.chat      import router as chat_router
from local_ai_law.routes.finder    import router as finder_router
from local_ai_law.routes.admin     import router as admin_router
from local_ai_law.routes.setup     import router as setup_router
from local_ai_law.routes.wiki      import router as wiki_router
from local_ai_law.routes.graph     import router as graph_router
from local_ai_law.routes.license   import router as license_router
from local_ai_law.routes.jobs      import router as jobs_router
from local_ai_law.routes.export    import router as export_router
from local_ai_law.routes.deprecated import router as deprecated_router

app.include_router(auth_router)
app.include_router(meta_router)
app.include_router(settings_router)
app.include_router(groups_router)
app.include_router(documents_router)
app.include_router(indexing_router)
app.include_router(sessions_router)
app.include_router(chat_router)
app.include_router(finder_router)
app.include_router(admin_router)
app.include_router(setup_router)
app.include_router(wiki_router)
app.include_router(graph_router)
app.include_router(license_router)
app.include_router(jobs_router)
app.include_router(export_router)
app.include_router(deprecated_router)


# ── Middleware ─────────────────────────────────────────────────────────────────

_PUBLIC_PATHS    = {"/", "/api/auth/login", "/api/auth/register",
                    "/api/auth/status", "/api/health", "/api/setup/status",
                    "/api/setup/editions", "/api/setup/complete",
                    "/api/setup/unlock",
                    # static UI constants + branding/disclaimer only — needed
                    # by the login screen before any token exists
                    "/api/meta"}
# NB: /api/setup/ is NOT a blanket public prefix — that exposed /api/setup/machine_id
# (a hardware fingerprint) without auth. The genuinely-public setup endpoints are
# listed individually in _PUBLIC_PATHS; the rest are reachable pre-setup only because
# the auth check is skipped while user_count()==0, and require auth afterwards.
_PUBLIC_PREFIXES = ("/static/", "/api/auth/")


@app.middleware("http")
async def auth_and_security(request: Request, call_next):
    from local_ai_law.services.metrics import inc_request_count
    inc_request_count()

    path     = request.url.path
    is_ws    = request.headers.get("upgrade", "").lower() == "websocket"
    is_public = path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES)

    # ── Lockout check (all API endpoints except setup/unlock) ────────────────
    if not is_public and not is_ws and path.startswith("/api/"):
        from local_ai_law.services.license import is_locked
        if is_locked():
            return JSONResponse(
                status_code=423,
                content={"detail": "Instance is locked. Contact your administrator.",
                         "code": "instance_locked"},
            )

    # ── Auth check ────────────────────────────────────────────────────────────
    if not is_public and not is_ws and path.startswith("/api/"):
        from local_ai_law.services.auth import get_user_from_header, verify_token, user_count
        if user_count() > 0:
            auth_header = request.headers.get("authorization")
            user = get_user_from_header(auth_header)
            # Fallback: read from httpOnly cookie when no Bearer header is present
            if not user:
                cookie_token = request.cookies.get("rag_token", "")
                if cookie_token:
                    user = verify_token(cookie_token)
            if not user:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated", "code": "unauthenticated"},
                )

    # ── License check (optional, only if licensing is enabled) ───────────────
    # Applied per-user on sensitive API calls; the per-route check is in routes/chat.py
    # (lightweight: only checks if module requires it, not every request)

    response: Response = await call_next(request)

    # Security headers — applied to every response
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]          = "SAMEORIGIN"
    response.headers["X-XSS-Protection"]         = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"]        = "camera=(), microphone=(), geolocation=()"
    # CSP: self-only sources. 'unsafe-inline' is required by the inline-script SPA
    # (static/index.html). 'unsafe-eval' is required by vendored libs that compile at
    # runtime — PDF.js (static/vendor/pdf.min.js + pdf.worker.min.js, document preview)
    # and D3 (static/vendor/d3.min.js, concept map) both use eval / new Function.
    # Dropping 'unsafe-eval' breaks PDF preview and the graph, so it is kept deliberately.
    # No remote script sources are permitted.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "connect-src 'self' ws: wss: blob:; "
        "worker-src 'self' blob:; "
        "frame-ancestors 'self';"
    )
    if HTTPS_ENABLED:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    if path.startswith("/static/"):
        # A versioned asset (?v=...) never changes under its URL; anything else is
        # revalidated by ETag on every load, so a client with no reload button (an
        # installed PWA) never keeps showing an old build.
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers.setdefault("Cache-Control", "no-store")

    return response


# The front door (services/guard.py): path walks, unknown Hosts and cross-site changes are
# refused before auth. Added last, so Starlette runs it first, for HTTP and WebSocket alike.
from local_ai_law.services.guard import FrontDoor  # noqa: E402
app.add_middleware(FrontDoor)


# ── Root ──────────────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "sw.js",
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


if __name__ == "__main__":
    import uvicorn
    ssl_args = {}
    if HTTPS_ENABLED:
        ssl_args = {"ssl_keyfile": KEY_FILE, "ssl_certfile": CERT_FILE}
    uvicorn.run("app:app", host=HOST, port=PORT, reload=True, **ssl_args)
