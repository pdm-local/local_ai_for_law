"""
Authentication service  -  JWT + Argon2id passwords, 100% local.
Users stored in a local JSON file (no external DB needed).

Password hashing:
  - New passwords: Argon2id (argon2-cffi).
  - Existing bcrypt hashes ($2b$) continue to work.
  - On successful login with a bcrypt hash, the hash is auto-migrated to Argon2id.
"""

import json
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import bcrypt
import jwt

from local_ai_law.config import BASE_DIR, DATA_DIR, log, migrate_legacy_data, atomic_write_json, atomic_write_text

# ── Brute-force protection ────────────────────────────────────────────────────
# Track failed attempts per (username, ip) within a rolling window.
# After _MAX_FAILS in _WINDOW_SECS the account is locked for _LOCKOUT_SECS.
# Persisted to disk (wall-clock timestamps) so a restart doesn't clear an
# in-progress lockout.
_FAIL_WINDOW_SECS = 600   # 10-minute sliding window
_MAX_FAILS        = 5     # attempts before lockout
_LOCKOUT_SECS     = 900   # 15-minute lockout
_fail_log: dict[str, list[float]] = defaultdict(list)  # key -> [timestamps]
_fail_lock = threading.Lock()
_LOCKOUTS_FILE = DATA_DIR / "security" / "lockouts.json"

def _load_fail_log():
    try:
        if _LOCKOUTS_FILE.exists():
            raw = json.loads(_LOCKOUTS_FILE.read_text())
            now = time.time()
            for key, ts in raw.items():
                if isinstance(ts, list):
                    live = [float(t) for t in ts if now - float(t) < _FAIL_WINDOW_SECS + _LOCKOUT_SECS]
                    if live:
                        _fail_log[key] = live
    except Exception:
        log.warning("Could not load lockout state from %s", _LOCKOUTS_FILE, exc_info=True)

def _save_fail_log_locked():
    """Persist the failure map. Caller must hold _fail_lock."""
    try:
        _LOCKOUTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(_LOCKOUTS_FILE, {k: v for k, v in _fail_log.items() if v})
    except Exception:
        log.warning("Could not persist lockout state", exc_info=True)

_load_fail_log()

def _fail_key(username: str, ip: str = "") -> str:
    """Brute-force tracking key. Keyed by username only (ip ignored) so an attacker
    can't dodge the lockout by rotating source IPs."""
    return username.lower()

def record_failed_login(username: str, ip: str = ""):
    """Record one failed login attempt for this user (pruning attempts older than the window)."""
    key = _fail_key(username, ip)
    now = time.time()
    with _fail_lock:
        attempts = [t for t in _fail_log[key] if now - t < _FAIL_WINDOW_SECS]
        attempts.append(now)
        _fail_log[key] = attempts
        _save_fail_log_locked()

def is_login_locked(username: str, ip: str = "") -> bool:
    """True if the user is currently locked out (>= _MAX_FAILS within the window, and
    still inside the _LOCKOUT_SECS cooldown after the latest attempt)."""
    key = _fail_key(username, ip)
    now = time.time()
    with _fail_lock:
        attempts = [t for t in _fail_log[key] if now - t < _FAIL_WINDOW_SECS]
        _fail_log[key] = attempts
        if len(attempts) >= _MAX_FAILS:
            # Locked until _LOCKOUT_SECS after the most recent attempt
            return (now - attempts[-1]) < _LOCKOUT_SECS
    return False

def clear_failed_logins(username: str, ip: str = ""):
    """Reset the failure counter for a user (called on a successful login)."""
    key = _fail_key(username, ip)
    with _fail_lock:
        if _fail_log.pop(key, None) is not None:
            _save_fail_log_locked()

# ── Argon2id setup ────────────────────────────────────────────────────────────

try:
    from argon2 import PasswordHasher as _ArgonPH
    from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
    _argon = _ArgonPH(
        time_cost=3,       # iterations
        memory_cost=65536, # 64 MiB
        parallelism=2,
        hash_len=32,
        salt_len=16,
    )
    _USE_ARGON = True
except ImportError:
    _USE_ARGON = False
    log.warning(
        "argon2-cffi not installed; new passwords will use bcrypt. "
        "Install argon2-cffi>=23.1.0 for Argon2id support."
    )

USERS_FILE = Path(os.getenv("RAG_USERS_FILE", str(BASE_DIR / "users.json")))
JWT_SECRET = os.getenv("RAG_JWT_SECRET", "")
JWT_ALGO = "HS256"
TOKEN_EXPIRE_HOURS = 72

if not JWT_SECRET:
    # Env-overridable so the containers persist it on the /app/data volume instead of
    # the ephemeral code dir (a code-dir secret is lost on `docker rm`, invalidating
    # every session). Defaults to the code dir for a plain native run.
    _secret_file = Path(os.getenv("RAG_JWT_SECRET_FILE", str(BASE_DIR / ".jwt_secret")))
    if _secret_file.exists():
        JWT_SECRET = _secret_file.read_text().strip()
        try:
            _secret_file.chmod(0o600)  # fix permissions if wrong from previous run
        except Exception:
            log.warning("Failed to fix JWT secret permissions", exc_info=True)
    else:
        JWT_SECRET = secrets.token_hex(32)
        atomic_write_text(_secret_file, JWT_SECRET)
        try:
            _secret_file.chmod(0o600)
        except Exception:
            log.warning("Failed to set JWT secret permissions", exc_info=True)
        log.info("Generated new JWT secret")


# ── Password helpers ──────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    """Hash a password with Argon2id (or bcrypt if argon2-cffi is unavailable)."""
    if _USE_ARGON:
        return _argon.hash(password)
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_password(password: str, stored_hash: str) -> bool:
    """Verify against Argon2id or bcrypt hash."""
    if stored_hash.startswith("$argon2"):
        if not _USE_ARGON:
            log.error(
                "Argon2id hash found but argon2-cffi is not installed. "
                "Install argon2-cffi>=23.1.0 to authenticate this user."
            )
            return False
        try:
            return _argon.verify(stored_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    # Fallback: bcrypt
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except Exception:
        return False


def _needs_rehash(stored_hash: str) -> bool:
    """True if the hash should be upgraded to current Argon2id parameters."""
    if not _USE_ARGON:
        return False
    if stored_hash.startswith("$argon2"):
        return _argon.check_needs_rehash(stored_hash)
    # bcrypt hash — should upgrade
    return True


# ── User store ────────────────────────────────────────────────────────────────

def _load_users() -> dict:
    """Load the local users.json store; returns {"users": []} if missing/corrupt."""
    if USERS_FILE.exists():
        try:
            return json.loads(USERS_FILE.read_text())
        except Exception:
            log.warning("Failed to load users file", exc_info=True)
            return {"users": []}
    return {"users": []}


def _save_users(data: dict):
    """Persist the users store atomically."""
    atomic_write_json(USERS_FILE, data)


def user_count() -> int:
    """Number of registered users (0 means setup hasn't run yet)."""
    return len(_load_users()["users"])


def validate_password_strength(password: str) -> str | None:
    """Return an error string if the password is too weak, else None."""
    if len(password) < 8:
        return "Password must be at least 8 characters"
    has_digit   = any(c.isdigit() for c in password)
    has_special = any(not c.isalnum() for c in password)
    if not (has_digit or has_special):
        return "Password must contain at least one number or special character"
    return None


def register_user(username: str, password: str, role: str = "user") -> dict | None:
    """Register a new user. Returns user dict or None if username taken."""
    data = _load_users()
    username = username.strip().lower()

    if any(u["username"] == username for u in data["users"]):
        return None

    user = {
        "username": username,
        "password_hash": _hash_password(password),
        "role": role,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    data["users"].append(user)
    _save_users(data)
    # Ensure users file is only readable by owner
    try:
        USERS_FILE.chmod(0o600)
    except Exception:
        log.warning("Failed to set users file permissions", exc_info=True)
    log.info("User registered: %s (role=%s)", username, role)

    if role == "admin":
        migrate_legacy_data(username)

    return {"username": username, "role": role}


def authenticate(username: str, password: str, ip: str = "") -> dict | None:
    """
    Verify credentials. Enforces brute-force lockout.
    Auto-migrates bcrypt hashes to Argon2id on successful login.
    Returns user dict (without hash) or None.
    """
    username_norm = username.strip().lower()

    if is_login_locked(username_norm, ip):
        log.warning("Login blocked (rate limit): user=%s ip=%s", username_norm, ip)
        return None

    data = _load_users()
    for u in data["users"]:
        if u["username"] != username_norm:
            continue
        stored = u["password_hash"]
        if not _verify_password(password, stored):
            record_failed_login(username_norm, ip)
            return None

        # Successful login — clear failure counter
        clear_failed_logins(username_norm, ip)

        # Auto-migrate hash if needed (bcrypt → argon2id, or rehash with new params)
        if _needs_rehash(stored):
            try:
                u["password_hash"] = _hash_password(password)
                _save_users(data)
                log.info("Password hash upgraded for user %s", username_norm)
            except Exception as e:
                log.warning("Hash migration failed for %s: %s", username_norm, e, exc_info=True)

        return {"username": u["username"], "role": u["role"]}

    record_failed_login(username_norm, ip)
    return None


def create_token(username: str, role: str) -> str:
    """Issue a signed JWT for the user, valid for TOKEN_EXPIRE_HOURS."""
    payload = {
        "sub": username,
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


def verify_token(token: str) -> dict | None:
    """Returns {"sub": username, "role": role} or None."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGO])
        return {"sub": payload["sub"], "role": payload["role"]}
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def get_user_from_header(authorization: str | None) -> dict | None:
    """Extract and verify the Bearer token from an Authorization header; None if absent/invalid."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return verify_token(authorization[7:])


def list_users() -> list[dict]:
    """List users (username/role/created) — never returns password hashes."""
    data = _load_users()
    return [
        {"username": u["username"], "role": u["role"], "created": u.get("created", "")}
        for u in data["users"]
    ]


def change_password(username: str, new_password: str) -> bool:
    """Set a new password hash for the user; True if the user existed."""
    data = _load_users()
    for u in data["users"]:
        if u["username"] == username:
            u["password_hash"] = _hash_password(new_password)
            _save_users(data)
            log.info("Password changed for user %s", username)
            return True
    return False
