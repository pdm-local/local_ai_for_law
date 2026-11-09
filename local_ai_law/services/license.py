"""
License service  -  Ed25519 offline verification + instance lockout.

Architecture:
  - Off unless a public key is configured; this open source release ships none.
  - An operator who wants it generates an Ed25519 key pair and signs license JSON.
  - Private key stays with the operator; never shipped.
  - Public key is embedded here (or set via RAG_LICENSE_PUBKEY env var).
  - License files are JSON payloads + base64url Ed25519 signatures.
  - Verification is fully offline.
  - Sensitive license store at rest is Fernet-encrypted (AES-128-GCM + HMAC).

Lockout:
  - After RAG_LOCK_ATTEMPTS (default 3) consecutive bad unlock passwords,
    lock_state.json sets locked=True.
  - No code or data is deleted.
  - A signed operator recovery token clears the lock.

Machine binding (optional):
  - If a license contains a non-null machine_id, the app computes a local
    fingerprint (CPU + MAC) and verifies it matches before granting access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import threading
import uuid
from datetime import datetime, timezone

from local_ai_law.config import DATA_DIR, log, atomic_write_json

# ── Lazy imports for cryptography (optional dep path) ─────────────────────────

def _load_crypto():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        from cryptography.exceptions import InvalidSignature
        from cryptography.fernet import Fernet
        return Ed25519PublicKey, load_der_public_key, InvalidSignature, Fernet
    except ImportError as e:
        log.warning("cryptography library not available; license verification disabled: %s", e)
        return None, None, None, None


# ── Paths ─────────────────────────────────────────────────────────────────────

LICENSE_DIR = DATA_DIR / "license"
LOCK_STATE_FILE = LICENSE_DIR / "lock_state.json"
USER_LICENSES_DIR = LICENSE_DIR / "user_licenses"
INSTANCE_LICENSE_FILE = LICENSE_DIR / "instance_license.enc"
PUBKEY_FILE = LICENSE_DIR / "verification_key.pub"

# Operator sets this at build/deploy time.
# Base64-encoded DER bytes of an Ed25519 public key.
_BUILTIN_PUBKEY_B64: str = os.getenv("RAG_LICENSE_PUBKEY", "")

MAX_ATTEMPTS = int(os.getenv("RAG_LOCK_ATTEMPTS", "3"))

_lock = threading.Lock()

# ── Fernet key for encrypting license store ───────────────────────────────────

def _get_fernet_key() -> bytes | None:
    """
    Load or generate a Fernet key for encrypting license files at rest.
    Key is stored in data/license/.fernet_key (chmod 600 recommended).
    """
    _, _, _, Fernet = _load_crypto()
    if Fernet is None:
        return None
    key_file = LICENSE_DIR / ".fernet_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if key_file.exists():
        return key_file.read_bytes().strip()
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except Exception:
        log.debug("Could not chmod license key file to 0600", exc_info=True)
    return key


def _encrypt(data: bytes) -> bytes:
    _, _, _, Fernet = _load_crypto()
    if Fernet is None:
        return data  # no-op if cryptography not available
    key = _get_fernet_key()
    if not key:
        return data
    return Fernet(key).encrypt(data)


def _decrypt(data: bytes) -> bytes:
    _, _, _, Fernet = _load_crypto()
    if Fernet is None:
        return data
    key = _get_fernet_key()
    if not key:
        return data
    try:
        return Fernet(key).decrypt(data)
    except Exception as e:
        raise ValueError("License file decryption failed — key mismatch or file corrupted") from e


# ── Public key loading ────────────────────────────────────────────────────────

def _load_public_key():
    """Return the Ed25519PublicKey object, or None if not configured."""
    Ed25519PublicKey, load_der_public_key, _, _ = _load_crypto()
    if load_der_public_key is None:
        return None

    b64 = _BUILTIN_PUBKEY_B64.strip()
    if not b64 and PUBKEY_FILE.exists():
        b64 = PUBKEY_FILE.read_text().strip()
    if not b64:
        return None

    try:
        der_bytes = base64.b64decode(b64)
        return load_der_public_key(der_bytes)
    except Exception as e:
        log.error("Failed to load Ed25519 public key: %s", e)
        return None


# ── License verification ──────────────────────────────────────────────────────

def _canonical_payload(payload: dict) -> bytes:
    """Deterministic JSON serialization for signing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def verify_license(license_data: dict, check_machine: bool = True) -> dict:
    """
    Verify a license dict with structure:
      {"payload": {...}, "signature": "<base64url>"}

    Returns the payload dict on success.
    Raises ValueError with a descriptive reason on any failure.
    """
    _, _, InvalidSignature, _ = _load_crypto()
    pubkey = _load_public_key()

    payload = license_data.get("payload")
    sig_b64 = license_data.get("signature", "")

    if not payload or not sig_b64:
        raise ValueError("malformed_license")

    # ── Signature check ──────────────────────────────────────────────────────
    if pubkey is not None and InvalidSignature is not None:
        try:
            sig_bytes = base64.urlsafe_b64decode(sig_b64 + "==")
            pubkey.verify(sig_bytes, _canonical_payload(payload))
        except Exception as e:
            raise ValueError(f"invalid_signature: {e}") from e
    else:
        raise ValueError("License verification failed: public key not configured")

    # ── Expiry check ─────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(payload["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if now > exp:
            raise ValueError("license_expired")
    except KeyError as e:
        raise ValueError("malformed_license: missing expires_at") from e

    # ── Machine binding check ─────────────────────────────────────────────────
    if check_machine:
        required_machine = payload.get("machine_id")
        if required_machine:
            local_id = get_machine_id()
            if local_id != required_machine:
                raise ValueError("machine_binding_mismatch")

    return payload


def load_user_license(username: str) -> dict | None:
    """Load and verify a per-user license. Returns payload or None."""
    from local_ai_law.config import validate_safe_id
    try:
        validate_safe_id(username, "username")
    except ValueError:
        return None
    lic_file = USER_LICENSES_DIR / f"{username}.lic"
    if not lic_file.exists():
        return None
    try:
        raw = _decrypt(lic_file.read_bytes())
        data = json.loads(raw)
        return verify_license(data)
    except ValueError as e:
        log.warning("License check failed for user %s: %s", username, e)
        return None
    except Exception as e:
        log.error("License load error for user %s: %s", username, e)
        return None


def save_user_license(username: str, license_data: dict) -> dict:
    """
    Verify then store a license for a user.
    Returns the validated payload.
    """
    from local_ai_law.config import validate_safe_id
    validate_safe_id(username, "username")   # never let a username escape the license dir
    payload = verify_license(license_data)
    USER_LICENSES_DIR.mkdir(parents=True, exist_ok=True)
    lic_file = USER_LICENSES_DIR / f"{username}.lic"
    raw = json.dumps(license_data, ensure_ascii=True).encode()
    lic_file.write_bytes(_encrypt(raw))
    try:
        lic_file.chmod(0o600)
    except Exception:
        log.debug("Could not chmod license file to 0600", exc_info=True)
    log.info("License saved for user %s (expires %s)", username, payload.get("expires_at"))
    return payload


def is_licensing_enabled() -> bool:
    """True if a public key is configured, meaning license checks are active."""
    return bool(_BUILTIN_PUBKEY_B64.strip() or PUBKEY_FILE.exists())


# ── Machine fingerprint ───────────────────────────────────────────────────────

def get_machine_id() -> str:
    """
    Compute a stable machine fingerprint from platform facts.
    Not cryptographically binding but adds friction.
    """
    parts = [
        platform.node(),
        str(uuid.getnode()),       # MAC address integer
        platform.machine(),
        platform.processor(),
    ]
    raw = "|".join(p for p in parts if p)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ── Lockout state ─────────────────────────────────────────────────────────────

def _load_lock_state() -> dict:
    LOCK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_STATE_FILE.exists():
        try:
            return json.loads(LOCK_STATE_FILE.read_text())
        except Exception:
            log.debug("Unreadable lock-state file; assuming unlocked", exc_info=True)
    return {"locked": False, "failed_attempts": 0, "locked_at": None}


def _save_lock_state(state: dict):
    LOCK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(LOCK_STATE_FILE, state)


def is_locked() -> bool:
    return _load_lock_state().get("locked", False)


def record_failed_attempt() -> dict:
    """
    Increment failed attempt counter.
    If MAX_ATTEMPTS reached, set locked=True.
    Returns updated state.
    """
    with _lock:
        state = _load_lock_state()
        state["failed_attempts"] = state.get("failed_attempts", 0) + 1
        if state["failed_attempts"] >= MAX_ATTEMPTS:
            state["locked"] = True
            state["locked_at"] = datetime.now(timezone.utc).isoformat()
            log.warning(
                "Instance locked after %d failed unlock attempts", state["failed_attempts"]
            )
        _save_lock_state(state)
        return state


def reset_attempts():
    """Reset counter after a successful unlock (correct password path)."""
    with _lock:
        state = _load_lock_state()
        state["failed_attempts"] = 0
        _save_lock_state(state)


def unlock_with_token(token_data: dict) -> bool:
    """
    Unlock using a signed operator recovery token.
    Token payload must have type="unlock" and a future expires_at.
    Uses the same Ed25519 public key as licenses.
    Returns True on success.
    """
    try:
        payload = verify_license(token_data, check_machine=False)
        if payload.get("type") != "unlock":
            log.warning("Unlock token has wrong type: %s", payload.get("type"))
            return False
    except ValueError as e:
        log.warning("Unlock token verification failed: %s", e)
        return False

    with _lock:
        state = _load_lock_state()
        state["locked"] = False
        state["failed_attempts"] = 0
        state["locked_at"] = None
        _save_lock_state(state)
    log.info("Instance unlocked via signed recovery token")
    return True


def get_lock_status() -> dict:
    state = _load_lock_state()
    return {
        "locked": state.get("locked", False),
        "failed_attempts": state.get("failed_attempts", 0),
        "max_attempts": MAX_ATTEMPTS,
        "locked_at": state.get("locked_at"),
        "licensing_enabled": is_licensing_enabled(),
    }
