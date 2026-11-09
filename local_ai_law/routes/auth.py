"""Auth endpoints  -  register, login, me, plus shared auth dependencies."""

from fastapi import APIRouter, HTTPException, Depends, Header, Request, Response
from pydantic import BaseModel, Field
from typing import Optional

from local_ai_law.config import log
from local_ai_law.services.auth import (
    register_user, authenticate, create_token, verify_token,
    get_user_from_header, user_count, validate_password_strength, is_login_locked,
    TOKEN_EXPIRE_HOURS,
)

_COOKIE_NAME    = "rag_token"
_COOKIE_MAX_AGE = 60 * 60 * TOKEN_EXPIRE_HOURS   # matches token expiry (72h)
from local_ai_law.services.audit import audit_log

router = APIRouter(prefix="/api/auth", tags=["auth"])


class AuthIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=50, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=128)
    language: Optional[str] = Field(None, pattern=r"^(en|nl)$")


def _resolve_user(authorization: Optional[str], request: Request) -> dict | None:
    """Try Bearer header first, then httpOnly cookie."""
    user = get_user_from_header(authorization)
    if user:
        return user
    cookie_token = request.cookies.get(_COOKIE_NAME, "")
    return verify_token(cookie_token) if cookie_token else None


# ── Shared dependencies (used by all routes) ─────────────────────────────────

def get_current_username(request: Request, authorization: Optional[str] = Header(None)) -> str:
    """Extract username from JWT (Bearer header or httpOnly cookie). Returns 'default' if no auth."""
    if user_count() == 0:
        return "default"
    user = _resolve_user(authorization, request)
    return user["sub"] if user else "default"


def require_auth(request: Request, authorization: Optional[str] = Header(None)) -> dict:
    """Require valid JWT from Bearer header or httpOnly cookie. Open-mode returns admin dict."""
    if user_count() == 0:
        return {"sub": "default", "role": "admin"}
    user = _resolve_user(authorization, request)
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user


def require_admin(user: dict = Depends(require_auth)) -> dict:
    """Dependency — require the authenticated user to have the admin role (403 otherwise)."""
    if user.get("role") != "admin":
        raise HTTPException(403, "Admin access required")
    return user


# ── Endpoints ────────────────────────────────────────────────────────────────

def _set_auth_cookie(response: Response, token: str):
    from local_ai_law.config import HTTPS_ENABLED
    response.set_cookie(
        key=_COOKIE_NAME,
        value=token,
        max_age=_COOKIE_MAX_AGE,
        httponly=True,
        samesite="strict",
        secure=HTTPS_ENABLED,
        path="/",
    )


@router.post("/register")
async def api_register(
    body: AuthIn, request: Request, response: Response,
    _admin: dict = Depends(require_admin),
):
    """POST — register a user. The FIRST user (no accounts yet) is created anonymously and
    becomes admin (bootstrap). After that, registration is **admin-only**: on a law-firm
    appliance, accounts are provisioned by an admin (each lawyer's walled account), not
    self-served — open registration would let anyone reachable on the network create
    accounts and pollute the audit trail. `require_admin` returns admin in open mode
    (user_count==0) so the bootstrap path stays anonymous. 409 if taken, 422 weak password."""
    pw_err = validate_password_strength(body.password)
    if pw_err:
        raise HTTPException(422, pw_err)
    role = "admin" if user_count() == 0 else "user"
    result = register_user(body.username, body.password, role=role)
    if not result:
        raise HTTPException(409, "Username already taken")
    # Persist the account's chosen language as its default (minimal per-user override).
    if body.language:
        try:
            import json
            from local_ai_law.config import user_data_dir, save_settings_file
            user_sf = user_data_dir(result["username"]) / "settings.json"
            override = {}
            if user_sf.exists():
                override = json.loads(user_sf.read_text())
            override["app_language"] = body.language
            save_settings_file(override, result["username"])
        except Exception:
            log.debug("Could not persist initial app_language for %s", result["username"], exc_info=True)
    token = create_token(result["username"], result["role"])
    _set_auth_cookie(response, token)
    audit_log(result["username"], "register", ip=request.client.host if request.client else "")
    try:
        from local_ai_law.services.scheduler import register_user_in_registry
        register_user_in_registry(result["username"])
    except Exception:
        log.debug("User-registry update failed after register", exc_info=True)
    return {"user": result, "token": token}


@router.post("/login")
async def api_login(body: AuthIn, request: Request, response: Response):
    """POST (public) — authenticate; sets the auth cookie and returns a token.
    429 when rate-limited, 401 on bad credentials."""
    ip = request.client.host if request.client else ""
    if is_login_locked(body.username.strip().lower(), ip):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = authenticate(body.username, body.password, ip=ip)
    if not user:
        audit_log(body.username, "login_failed", ip=ip)
        raise HTTPException(401, "Invalid credentials")
    token = create_token(user["username"], user["role"])
    _set_auth_cookie(response, token)
    audit_log(user["username"], "login", ip=ip)
    try:
        from local_ai_law.services.scheduler import register_user_in_registry
        register_user_in_registry(user["username"])
    except Exception:
        log.debug("User-registry update failed after login", exc_info=True)
    return {"user": user, "token": token}


@router.post("/logout")
async def api_logout(response: Response):
    """POST — clear the auth cookie."""
    response.delete_cookie(key=_COOKIE_NAME, path="/")
    return {"message": "Logged out"}


@router.get("/me")
async def api_me(user: dict = Depends(require_auth)):
    """GET — the current authenticated user's username and role."""
    return {"user": {"username": user["sub"], "role": user["role"]}}


@router.get("/status")
async def api_auth_status():
    """Public endpoint: tells the frontend if auth is set up."""
    count = user_count()
    return {"has_users": count > 0, "user_count": count}


@router.post("/refresh")
async def api_refresh_token(response: Response, user: dict = Depends(require_auth)):
    """Exchange a valid token for a new one with a fresh expiry. Call before token expires."""
    new_token = create_token(user["sub"], user["role"])
    _set_auth_cookie(response, new_token)
    return {"token": new_token}
