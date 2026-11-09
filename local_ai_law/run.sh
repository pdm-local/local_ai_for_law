#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  RAG Studio  -  single-file runner
#  One command to install, check, and start the app. 100% local, Ollama only.
#
#  Usage:
#    ./run.sh              # full setup + start (idempotent)
#    ./run.sh install      # install deps only
#    ./run.sh start        # start (skip install)  -  assumes venv is ready
#    ./run.sh docker       # run via docker compose instead
#    ./run.sh stop         # kill any running instance
#    ./run.sh update       # pull latest deps + restart
#    ./run.sh --help
# ══════════════════════════════════════════════════════════════════════════

set -e
cd "$(dirname "$0")"

# ── Colors ──────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'; C_RED=$'\033[0;31m'
  C_BLUE=$'\033[0;34m';  C_DIM=$'\033[2m';        C_RESET=$'\033[0m'
else
  C_GREEN=; C_YELLOW=; C_RED=; C_BLUE=; C_DIM=; C_RESET=
fi
say()  { echo "${C_BLUE}▸${C_RESET} $*"; }
ok()   { echo "${C_GREEN}✓${C_RESET} $*"; }
warn() { echo "${C_YELLOW}⚠${C_RESET} $*"; }
err()  { echo "${C_RED}✗${C_RESET} $*" >&2; }
dim()  { echo "${C_DIM}$*${C_RESET}"; }

# ── Config ─────────────────────────────────────────────────────────────
VENV_DIR="venv"
PY_MIN="3.10"
PID_FILE=".rag_studio.pid"

# Load .env if present
if [ -f .env ]; then
  set -a; source .env; set +a
  dim "Loaded .env"
fi

# Loopback by default; `--lan` (any position) opts in to LAN exposure for the
# phone/tablet demo. Explicit RAG_HOST always wins.
for _arg in "$@"; do
  if [ "$_arg" = "--lan" ] && [ -z "${RAG_HOST:-}" ]; then
    RAG_HOST="0.0.0.0"
  fi
done
HOST="${RAG_HOST:-127.0.0.1}"
export RAG_HOST="$HOST"
PORT="${RAG_PORT:-8000}"
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
if [[ "$OLLAMA_HOST" =~ ^[0-9]+$ ]]; then
  OLLAMA_HOST="http://127.0.0.1:$OLLAMA_HOST"
elif [[ "$OLLAMA_HOST" != *"://"* ]]; then
  OLLAMA_HOST="http://$OLLAMA_HOST"
fi
export OLLAMA_HOST

# ── Helpers ────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
RAG Studio  -  single-file runner

Usage:
  ./run.sh                 Full setup + start (idempotent, recommended for first run)
  ./run.sh install         Create venv + install deps, don't start
  ./run.sh start           Start server (assumes install is done)
  ./run.sh start --lan     Start reachable over LAN (phone/tablet demo; default is loopback-only)
  ./run.sh stop            Stop a running instance
  ./run.sh restart         Stop then start
  ./run.sh update          Upgrade pip deps + restart
  ./run.sh docker          Run via docker compose (alternative)
  ./run.sh clean           Remove venv (does NOT touch user data)
  ./run.sh doctor          Diagnose environment
  ./run.sh build-chat      Rebuild the React chat assets
  ./run.sh -h, --help      This help

Environment (via .env or shell):
  RAG_HOST          (default 127.0.0.1; use --lan or RAG_HOST=0.0.0.0 for LAN access)
  RAG_PORT          (default 8000)
  PDM_HOSTS         (LAN names the box is reached by, comma separated; others get a 400)
  OLLAMA_HOST       (default http://127.0.0.1:11434; set in .env for custom ports)
  RAG_MAX_UPLOAD_MB     (default 50)
  RAG_LOG_LEVEL         (default INFO)

Data paths (all local, never leave this machine):
  ./uploads/       -  your uploaded documents
  ./rag_db/        -  ChromaDB vector store
  ./chat_history/  -  saved chat sessions
  ./overviews/     -  generated group overviews
  ./doc_summaries/ -  per-document AI summaries
  ./flashcards/    -  spaced-repetition cards
EOF
}

find_python() {
  for p in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$p" >/dev/null 2>&1; then
      if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        echo "$p"; return 0
      fi
    fi
  done
  return 1
}

check_python() {
  PY=$(find_python) || {
    err "Python >= ${PY_MIN} not found. Install it first:"
    echo "   Ubuntu/Debian: sudo apt install python3.12 python3.12-venv"
    echo "   Fedora:        sudo dnf install python3.12"
    echo "   macOS:         brew install python@3.12"
    exit 1
  }
  ok "Python: $($PY --version) ($(command -v $PY))"
}

ensure_venv() {
  if [ ! -f "$VENV_DIR/bin/python" ]; then
    say "Creating virtual environment in $VENV_DIR/"
    "$PY" -m venv "$VENV_DIR"
    ok "venv created"
  else
    dim "venv already exists"
  fi
}

install_deps() {
  say "Installing dependencies (this can take a few minutes on first run)"
  "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
  "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
  "$VENV_DIR/bin/python" -m pip install --quiet -r requirements.txt
  ok "Dependencies installed"
}

check_ollama() {
  local host="${OLLAMA_HOST}"
  say "Checking Ollama at $host"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "$host/api/tags" >/dev/null 2>&1; then
      ok "Ollama is running"
      local models
      models=$(curl -fsS --max-time 3 "$host/api/tags" 2>/dev/null | grep -o '"name":"[^"]*"' | head -5 | sed 's/"name":"//; s/"$//')
      if [ -n "$models" ]; then
        dim "   Installed models:"
        echo "$models" | sed 's/^/     · /'
      else
        warn "No Ollama models installed. Pull a chat model and an embedding model via: ollama pull <model>"
      fi
      return 0
    fi
  fi
  warn "Ollama not reachable at $host"
  echo "   The app will still start, but chat/indexing will fail until Ollama is running."
  echo "   Install & start Ollama: https://ollama.com/download"
  echo "   Set OLLAMA_HOST in .env if using a non-default address."
  return 1
}

ensure_dirs() {
  mkdir -p uploads rag_db chat_history overviews doc_summaries flashcards
}

ensure_chat_assets() {
  if [ -f static/chat/chat.js ] && [ -f static/chat/chat.css ]; then
    dim "chat assets ready"
    return 0
  fi
  warn "React chat assets are missing"
  if command -v npm >/dev/null 2>&1 && [ -f frontend/package.json ]; then
    say "Building chat assets"
    (cd frontend && npm run build)
    ok "Chat assets built"
  else
    err "Cannot build chat assets automatically. Install Node/npm, then run: ./run.sh build-chat"
    exit 1
  fi
}

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat $PID_FILE)" 2>/dev/null
}

do_start() {
  if is_running; then
    warn "Already running (pid $(cat $PID_FILE)) — restarting"
    do_stop
  fi
  # Kill any stale process on the port
  local stale_pid
  stale_pid=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
  if [ -n "$stale_pid" ]; then
    warn "Port $PORT in use by pid $stale_pid — killing"
    kill "$stale_pid" 2>/dev/null || true
    sleep 1
  fi
  ensure_dirs
  ensure_chat_assets
  say "Starting RAG Studio"
  ok "Open: http://127.0.0.1:$PORT"
  dim "Network: http://$HOST:$PORT"
  dim "(Ctrl+C to stop)"
  echo ""
  exec "$VENV_DIR/bin/python" -m uvicorn app:app --host "$HOST" --port "$PORT"
}

do_start_bg() {
  if is_running; then
    do_stop
  fi
  local stale_pid
  stale_pid=$(lsof -ti tcp:"$PORT" 2>/dev/null || true)
  if [ -n "$stale_pid" ]; then
    warn "Port $PORT in use by pid $stale_pid — killing"
    kill "$stale_pid" 2>/dev/null || true
    sleep 1
  fi
  ensure_dirs
  ensure_chat_assets
  say "Starting RAG Studio in background"
  nohup "$VENV_DIR/bin/python" -m uvicorn app:app --host "$HOST" --port "$PORT" \
    > rag_studio.log 2>&1 &
  echo $! > "$PID_FILE"
  sleep 2
  if is_running; then
    ok "Running (pid $(cat $PID_FILE)). Logs: rag_studio.log"
    ok "Open: http://127.0.0.1:$PORT"
  else
    err "Failed to start. Check rag_studio.log"
    rm -f "$PID_FILE"
    exit 1
  fi
}

do_stop() {
  if is_running; then
    say "Stopping (pid $(cat $PID_FILE))"
    kill "$(cat $PID_FILE)" 2>/dev/null || true
    sleep 1
    if is_running; then
      warn "Still running, sending SIGKILL"
      kill -9 "$(cat $PID_FILE)" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    ok "Stopped"
  else
    if [ -f "$PID_FILE" ]; then
      warn "Removing stale PID file"
      rm -f "$PID_FILE"
    else
      dim "Nothing to stop"
    fi
  fi
}

do_install() {
  check_python
  ensure_venv
  install_deps
  ensure_dirs
  ensure_chat_assets
  ok "Install complete. Run './run.sh start' to launch."
}

do_build_chat() {
  command -v npm >/dev/null 2>&1 || { err "npm not found. Install Node.js first."; exit 1; }
  [ -f frontend/package.json ] || { err "frontend/package.json not found"; exit 1; }
  say "Building React chat assets"
  (cd frontend && npm run build)
  ok "Chat assets written to static/chat/"
}

do_doctor() {
  echo "═════ RAG Studio  -  Environment Report ═════"
  check_python || true
  if [ -f "$VENV_DIR/bin/python" ]; then
    ok "venv present"
    dim "   $("$VENV_DIR/bin/python" --version)"
  else
    warn "venv missing  -  run './run.sh install'"
  fi
  if "$VENV_DIR/bin/python" -m uvicorn --version >/dev/null 2>&1; then
    ok "uvicorn installed"
  else
    warn "uvicorn missing"
  fi
  check_ollama || true
  if command -v docker >/dev/null 2>&1; then
    ok "docker available  -  './run.sh docker' works"
  fi
  for d in uploads rag_db chat_history overviews doc_summaries flashcards; do
    if [ -d "$d" ]; then
      dim "   ./$d/ ($(du -sh "$d" 2>/dev/null | cut -f1))"
    fi
  done
  if is_running; then
    ok "Running (pid $(cat $PID_FILE))"
  fi
}

do_docker() {
  command -v docker >/dev/null 2>&1 || { err "Docker not installed"; exit 1; }
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    err "docker compose not available"
    exit 1
  fi
  say "Starting via $COMPOSE"
  exec $COMPOSE up --build
}

do_clean() {
  warn "This will remove $VENV_DIR/ but KEEP your data (uploads, rag_db, etc.)"
  read -p "Continue? [y/N] " ans
  if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    rm -rf "$VENV_DIR"
    ok "Cleaned"
  else
    dim "Cancelled"
  fi
}

# ── Dispatch ───────────────────────────────────────────────────────────
cmd="${1:-all}"
case "$cmd" in
  -h|--help|help)
    usage
    ;;
  install)
    do_install
    ;;
  start)
    if ! "$VENV_DIR/bin/python" -m uvicorn --version >/dev/null 2>&1; then
      warn "venv not ready  -  running install first"
      do_install
    fi
    check_ollama || true
    do_start
    ;;
  start-bg|bg)
    if ! "$VENV_DIR/bin/python" -m uvicorn --version >/dev/null 2>&1; then
      do_install
    fi
    check_ollama || true
    do_start_bg
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_stop
    do_start_bg
    ;;
  update)
    check_python
    ensure_venv
    say "Upgrading dependencies"
    "$VENV_DIR/bin/python" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
    "$VENV_DIR/bin/python" -m pip install --quiet --upgrade -r requirements.txt
    ok "Updated"
    if is_running; then
      do_stop
      do_start_bg
    fi
    ;;
  docker)
    do_docker
    ;;
  doctor|diag|diagnose)
    do_doctor
    ;;
  clean)
    do_clean
    ;;
  build-chat)
    do_build_chat
    ;;
  all|"")
    # Default: full install + foreground start
    check_python
    ensure_venv
    install_deps
    ensure_dirs
    ensure_chat_assets
    check_ollama || true
    do_start
    ;;
  *)
    err "Unknown command: $cmd"
    echo ""
    usage
    exit 1
    ;;
esac
