#!/usr/bin/env bash
# Run the app against a throwaway data dir, so UI work and screenshots never touch
# the real data/, users.json, audit.log or JWT secret. Binds 127.0.0.1 only.
#
#   scripts/sandbox.sh            # start on http://127.0.0.1:8380
#   SANDBOX_DIR=/tmp/x scripts/sandbox.sh
#   scripts/sandbox.sh reset      # wipe the sandbox dir first
#
# Signs in as demo / meridiaan-demo-2025 (role user, so no legacy-data migration runs).
set -euo pipefail
cd "$(dirname "$0")/.."

SANDBOX_DIR="${SANDBOX_DIR:-/tmp/pdm-law-sandbox}"
PORT="${PDM_PORT:-8380}"

if [[ "${1:-}" == "reset" ]]; then rm -rf "$SANDBOX_DIR"; fi
mkdir -p "$SANDBOX_DIR"
chmod 700 "$SANDBOX_DIR"

export RAG_DATA_DIR="$SANDBOX_DIR/data"
export RAG_USERS_FILE="$SANDBOX_DIR/users.json"
export RAG_AUDIT_LOG="$SANDBOX_DIR/audit.log"
export RAG_SETTINGS_FILE="$SANDBOX_DIR/settings.json"
export RAG_JWT_SECRET_FILE="$SANDBOX_DIR/.jwt_secret"

# The package imports itself as local_ai_law, so its parent goes on the path.
export PYTHONPATH="$(cd .. && pwd)${PYTHONPATH:+:$PYTHONPATH}"
PY="${PYTHON:-venv/bin/python}"
"$PY" -c '
from local_ai_law.services.auth import _load_users, register_user
from local_ai_law.config import load_instance, save_instance
if not load_instance().get("setup_complete"):
    save_instance({"setup_complete": True, "instance_name": "Sandbox"})
if not any(u["username"] == "demo" for u in _load_users()["users"]):
    register_user("demo", "meridiaan-demo-2025", role="user")
'

exec "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT"
