#!/usr/bin/env bash
#
# appliance-entrypoint.sh — PID 1 for the self-contained pdm.ai appliance image.
# Starts the baked-in Ollama server, waits for it, ensures a persistent JWT
# secret in the data volume, then execs gunicorn as the app.
set -euo pipefail

: "${RAG_DATA_DIR:=/app/data}"
: "${OLLAMA_HOST:=127.0.0.1:11434}"
: "${APP_HOME:=/app/local_ai_law}"

# Everything written to the data volume (documents, indexes, audit log) is owner-only:
# on a host bind mount, other local accounts must not read a firm's files.
umask 077

log() { printf '\033[1;36m[appliance]\033[0m %s\n' "$*"; }

# 1) Start Ollama (models are baked into $OLLAMA_MODELS) in the background.
log "starting ollama (models: ${OLLAMA_MODELS:-default})"
ollama serve &
OLLAMA_PID=$!

# 2) Wait for it to answer before bringing the app up (models load lazily on
#    first request, so we only need the API to be live here).
log "waiting for ollama on ${OLLAMA_HOST} ..."
for _ in $(seq 1 90); do
  if curl -fsS "http://${OLLAMA_HOST}/api/tags" >/dev/null 2>&1; then
    log "ollama is up"
    break
  fi
  if ! kill -0 "$OLLAMA_PID" 2>/dev/null; then
    echo "[appliance] ollama exited during startup" >&2
    exit 1
  fi
  sleep 1
done

# 3) Persist a JWT secret in the data volume (never bake one into the image).
#    If RAG_JWT_SECRET is already set, honor it; otherwise generate once and
#    reuse across container recreations so sessions survive.
mkdir -p "$RAG_DATA_DIR"
if [ -z "${RAG_JWT_SECRET:-}" ]; then
  SECRET_FILE="$RAG_DATA_DIR/.jwt_secret"
  if [ ! -s "$SECRET_FILE" ]; then
    log "generating a new JWT secret in the data volume"
    ( umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$SECRET_FILE" )
  fi
  RAG_JWT_SECRET="$(cat "$SECRET_FILE")"
  export RAG_JWT_SECRET
fi

# 4) Hand off to gunicorn from the app dir (replaces this shell so signals reach it).
#    Global settings persist on the data volume via RAG_SETTINGS_FILE (set in the image
#    ENV) — config.SETTINGS_F honours it, so no symlink into the app dir is needed.
cd "$APP_HOME"
log "starting pdm.ai on :${RAG_PORT:-8000}"
exec gunicorn -c gunicorn.conf.py app:app
