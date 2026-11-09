#!/usr/bin/env bash
#
# provision.sh — one command to turn a bare 12 GB-VRAM box into a running pdm Legal
# Workspace appliance. Idempotent; safe to re-run. This scripts EXACTLY what a deploy
# needs so the box is reproducible (and doubles as disaster recovery).
#
#   ./scripts/provision.sh                  # install deps + models + reranker cache + render unit
#   ./scripts/provision.sh --install-service# also install+enable+start the systemd service (uses sudo)
#
# Everything except --install-service is unprivileged. The privileged step is opt-in so a
# plain run never silently changes system state.
#
# Tunables (env): RAG_MODELS, OLLAMA_HOST, RAG_DATA_DIR, RAG_USERS_FILE, RAG_BIND_HOST, RAG_PORT
set -euo pipefail

# ── Portable paths: derive everything from this script's location ──────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"            # …/local_ai_law
PYTHONPATH_ROOT="$(cd "$APP_DIR/.." && pwd)"       # parent of local_ai_law (package import root)

# ── Config (override via env) ─────────────────────────────────────────────────
OLLAMA_HOST="${OLLAMA_HOST:-http://127.0.0.1:11434}"
DATA_DIR="${RAG_DATA_DIR:-$APP_DIR/data}"
USERS_FILE="${RAG_USERS_FILE:-$APP_DIR/users.json}"
BIND_HOST="${RAG_BIND_HOST:-127.0.0.1}"            # localhost-only by default (safe)
PORT="${RAG_PORT:-8000}"
SVC_USER="${SUDO_USER:-$(id -un)}"
SVC_GROUP="$(id -gn "$SVC_USER")"
# Ollama models the appliance needs. Chat + embed are mandatory; graph powers the concept
# map. The judge model is benchmark-only and intentionally omitted from a deploy box.
RAG_MODELS="${RAG_MODELS:-gemma4:e4b qwen3-embedding:4b gemma4:e2b}"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] && die "Do not run provision.sh as root — the appliance runs unprivileged. Re-run as the service user."

BUNDLE_DIR="$APP_DIR/deploy/ollama-bundle"

# Where the target's Ollama keeps its model store (blobs + manifests).
ollama_models_root() {
  if [ -n "${OLLAMA_MODELS:-}" ]; then echo "$OLLAMA_MODELS"; return; fi
  local l; l="$(systemctl show ollama -p Environment 2>/dev/null | tr ' ' '\n' | sed -n 's/^OLLAMA_MODELS=//p' | head -1)"
  [ -n "$l" ] && { echo "$l"; return; }
  for c in /usr/share/ollama/.ollama/models "$HOME/.ollama/models"; do
    [ -d "$c" ] && { echo "$c"; return; }
  done
  echo "/usr/share/ollama/.ollama/models"
}

# Import a pre-built model bundle (scripts/bundle_ollama_models.sh) into the target store —
# offline, no `ollama pull`, no internet. Privileged: the store is owned by the ollama user.
import_ollama_bundle() {
  [ -d "$BUNDLE_DIR/manifests" ] || return 1
  local root; root="$(ollama_models_root)"
  say "Importing bundled models offline → $root (no pull, no internet)"
  sudo mkdir -p "$root/blobs" "$root/manifests"
  sudo cp -rn "$BUNDLE_DIR/blobs/." "$root/blobs/" 2>/dev/null || true
  sudo cp -rn "$BUNDLE_DIR/manifests/." "$root/manifests/"
  local owner; owner="$(stat -c '%U:%G' "$root" 2>/dev/null || echo ollama:ollama)"
  sudo chown -R "$owner" "$root/blobs" "$root/manifests"
  sudo systemctl restart ollama 2>/dev/null || true
  for _ in $(seq 1 10); do ollama list >/dev/null 2>&1 && break; sleep 1; done
  while IFS= read -r m; do
    [ -n "$m" ] || continue
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then ok "imported: $m"
    else warn "model not visible after import: $m"; fi
  done < "$BUNDLE_DIR/MODELS.txt"
}

# Wall Ollama off from user data + the network. Ollama is only a model server (loopback HTTP);
# this drop-in makes it structurally unable to read the app's data/ or be reached off-box.
harden_ollama() {
  local tmpl="$APP_DIR/deploy/ollama-hardening.conf.template"
  [ -f "$tmpl" ] || { warn "ollama hardening template missing — skipping"; return 0; }
  local root dir="/etc/systemd/system/ollama.service.d"
  root="$(ollama_models_root)"
  say "Hardening Ollama: loopback bind + no access to user data (drop-in $dir/hardening.conf)"
  sudo mkdir -p "$dir"
  sed "s|__OLLAMA_MODELS__|$root|g" "$tmpl" | sudo tee "$dir/hardening.conf" >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl restart ollama 2>/dev/null || true
  case "$root" in
    /home/*) warn "Ollama model store is under /home — with ProtectHome=true Ollama can't read it. "\
                  "Move models off /home, or set ProtectHome=read-only in the drop-in." ;;
  esac
}

# Lock the app's data dir so only the app's service user can read it (defence in depth: even
# without the systemd walls, the OS denies other local users — incl. the ollama user — access).
lock_data_dir() {
  [ -d "$DATA_DIR" ] || return 0
  say "Locking data dir to $SVC_USER only: chmod 700 $DATA_DIR"
  sudo chown -R "$SVC_USER:$SVC_GROUP" "$DATA_DIR"
  sudo chmod 700 "$DATA_DIR"
}

# ── 1. App deps (reuse run.sh — single source of truth for install) ───────────
say "Installing app + Python deps via run.sh"
( cd "$APP_DIR" && ./run.sh install )
ok "Dependencies installed"

# ── 2. Ollama models — prefer the offline bundle; else pull ───────────────────
# "Model already packed": if deploy/ollama-bundle exists it is imported offline during the
# privileged --install-service step (no internet). Otherwise we pull (needs network).
if ! command -v ollama >/dev/null 2>&1; then
  warn "ollama not found on PATH — install it (https://ollama.com) before go-live."
elif [ -d "$BUNDLE_DIR/manifests" ]; then
  say "Model bundle present → imported offline during --install-service (no pull):"
  [ -r "$BUNDLE_DIR/MODELS.txt" ] && sed 's/^/    • /' "$BUNDLE_DIR/MODELS.txt"
else
  say "No model bundle — pulling Ollama models: $RAG_MODELS"
  for m in $RAG_MODELS; do
    if ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$m"; then
      ok "model present: $m"
    else
      say "pulling $m"; ollama pull "$m" || warn "could not pull $m (pull it manually before go-live)"
    fi
  done
fi

# ── 3. Pre-cache the cross-encoder reranker — OPTIONAL enhancement ─────────────
# The cross-encoder (services/cross_encoder.py, gated by cross_encoder.available()) is an
# OPTIONAL retrieval reranker: it needs sentence_transformers + torch, which are deliberately
# NOT in requirements.txt (heavy, and the box runs fine without them — retrieval fails open to
# the hybrid RRF ordering, and the default rerank path is the Ollama one in services/reranker.py).
# So a base install intentionally ships with the cross-encoder OFF. Only pre-cache the model if
# the caller opted into the deps (pip install sentence_transformers torch); otherwise say so
# plainly instead of emitting a scary failure for a component that is off by design.
if PYTHONPATH="$PYTHONPATH_ROOT" "$APP_DIR/venv/bin/python" -c "import sentence_transformers, torch" 2>/dev/null; then
  say "Cross-encoder deps present → pre-caching BAAI/bge-reranker-v2-m3 for offline use"
  PYTHONPATH="$PYTHONPATH_ROOT" "$APP_DIR/venv/bin/python" - <<'PY' || warn "reranker pre-cache failed; it will download on first use (needs network then)"
from sentence_transformers import CrossEncoder
CrossEncoder("BAAI/bge-reranker-v2-m3", max_length=512)
print("reranker cached OK")
PY
else
  say "Cross-encoder OFF (optional): sentence_transformers/torch not installed — retrieval uses the "\
      "Ollama reranker + hybrid ordering. To enable it: pip install sentence_transformers torch, then re-run."
fi

# ── 4. Render the systemd unit from the template ──────────────────────────────
TEMPLATE="$APP_DIR/deploy/pdm-legal.service.template"
RENDERED="$APP_DIR/deploy/pdm-legal.service"
say "Rendering systemd unit → $RENDERED"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__USER__|$SVC_USER|g" \
    -e "s|__GROUP__|$SVC_GROUP|g" \
    -e "s|__PYTHONPATH__|$PYTHONPATH_ROOT|g" \
    -e "s|__OLLAMA_HOST__|$OLLAMA_HOST|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    -e "s|__USERS_FILE__|$USERS_FILE|g" \
    -e "s|__BIND_HOST__|$BIND_HOST|g" \
    -e "s|__PORT__|$PORT|g" \
    "$TEMPLATE" > "$RENDERED"
ok "Unit rendered (User=$SVC_USER, bind=$BIND_HOST:$PORT, data=$DATA_DIR)"

# ── 5. Optionally install + enable + start the service (privileged) ───────────
if [ "${1:-}" = "--install-service" ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemctl not found — this OS doesn't use systemd; run the app via ./run.sh start bg instead."
  # Offline model import (no internet) — only if a bundle was shipped with the appliance.
  import_ollama_bundle || say "No model bundle to import (using whatever Ollama already has)."
  # Wall Ollama off from user data + the network, and lock the data dir to the app user.
  harden_ollama
  lock_data_dir
  say "Installing service (sudo): /etc/systemd/system/pdm-legal.service"
  sudo cp "$RENDERED" /etc/systemd/system/pdm-legal.service
  sudo systemctl daemon-reload
  sudo systemctl enable pdm-legal.service        # survive reboot
  sudo systemctl restart pdm-legal.service
  say "Waiting for health…"
  for _ in $(seq 1 30); do
    if curl -fsS "http://$BIND_HOST:$PORT/api/health" >/dev/null 2>&1; then
      ok "Service healthy at http://$BIND_HOST:$PORT — enabled, will restart on boot/crash."; exit 0
    fi
    sleep 1
  done
  die "Service started but health check failed — check: journalctl -u pdm-legal -n 50"
else
  cat <<EOF

$(ok "Provisioning done.") To run the app as a managed service that survives reboot, install the unit:

    sudo cp "$RENDERED" /etc/systemd/system/pdm-legal.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now pdm-legal.service
    curl -fsS http://$BIND_HOST:$PORT/api/health

…or re-run:  ./scripts/provision.sh --install-service

Security notes:
  • Bind host is $BIND_HOST (localhost-only). For LAN access put a TLS reverse proxy in
    front (scripts/gen-cert.sh) rather than binding 0.0.0.0 without encryption.
  • The unit runs as $SVC_USER (non-root), read-only filesystem except $DATA_DIR + app tree.
EOF
fi
