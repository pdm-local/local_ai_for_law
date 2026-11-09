#!/usr/bin/env bash
#
# build_appliance.sh — ONE command to produce the self-contained pdm.ai Legal
# appliance image: the app + the Ollama server + the model weights, baked into a
# single `docker load`-able tarball. Move it to any NVIDIA box, `docker load`,
# `docker run --gpus all` — no `ollama pull`, no internet, no compose.
#
#   scripts/build_appliance.sh                    # build + save dist/pdm-legal-appliance.tar.gz
#   scripts/build_appliance.sh --skip-bundle      # reuse an existing deploy/ollama-bundle
#   scripts/build_appliance.sh --no-save          # build the image only (skip the tarball)
#   scripts/build_appliance.sh --tag pdm-legal:v1 # custom image tag
#   scripts/build_appliance.sh --to desktop       # also stream the image to an SSH host (Tailscale)
#
# Model set (must be pulled on THIS build box so they can be bundled):
#   RAG_CHAT_MODEL  (default qwen3.5:9b)
#   RAG_EMBED_MODEL (default qwen3-embedding:4b)
#   RAG_GRAPH_MODEL (default gemma4:e2b)
#
# The build needs internet + disk ONCE (base image + pip wheels); the RESULT is
# fully offline. Baking is done by COPYing deploy/ollama-bundle/ (native Ollama
# manifests+blobs) into the image's /models — see Dockerfile.appliance.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ── Config (override via env) ─────────────────────────────────────────────────
IMAGE_TAG="${IMAGE_TAG:-pdm-legal:appliance}"
CHAT_MODEL="${RAG_CHAT_MODEL:-qwen3.5:9b}"
EMBED_MODEL="${RAG_EMBED_MODEL:-qwen3-embedding:4b}"
GRAPH_MODEL="${RAG_GRAPH_MODEL:-gemma4:e2b}"
DIST_DIR="$APP_DIR/dist"
TARBALL="$DIST_DIR/pdm-legal-appliance.tar.gz"
BUNDLE_DIR="$APP_DIR/deploy/ollama-bundle"
DOCKERFILE="$APP_DIR/Dockerfile.appliance"

SKIP_BUNDLE=0
DO_SAVE=1
SSH_TO=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-bundle) SKIP_BUNDLE=1; shift ;;
    --no-save)     DO_SAVE=0; shift ;;
    --tag)         IMAGE_TAG="${2:-}"; [ -n "$IMAGE_TAG" ] || die "--tag needs a value"; shift 2 ;;
    --to)          SSH_TO="${2:-}"; [ -n "$SSH_TO" ] || die "--to needs an ssh host"; shift 2 ;;
    -h|--help)     grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1 (see --help)" ;;
  esac
done

# ── Preflight ─────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "docker not found — install Docker first"
docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (is it running? are you in the docker group?)"
[ -f "$DOCKERFILE" ] || die "missing $DOCKERFILE"

# ── 1. Ensure the baked model bundle exists (native Ollama layout) ────────────
if [ "$SKIP_BUNDLE" -eq 0 ]; then
  say "Bundling models for the image: $CHAT_MODEL  $EMBED_MODEL  $GRAPH_MODEL"
  # bundle_ollama_models.sh reads THIS box's Ollama store — the models must be pulled here.
  "$SCRIPT_DIR/bundle_ollama_models.sh" "$CHAT_MODEL" "$EMBED_MODEL" "$GRAPH_MODEL" \
    || die "bundling failed — are all three models pulled on this box? ('ollama pull ...')"
else
  say "Reusing existing bundle (--skip-bundle)"
fi
[ -d "$BUNDLE_DIR/manifests" ] || die "no model bundle at $BUNDLE_DIR (drop --skip-bundle, or run bundle_ollama_models.sh)"
# Fail loud if the chat model the Dockerfile expects isn't actually in the bundle.
if [ -f "$BUNDLE_DIR/MODELS.txt" ]; then
  grep -qx "$CHAT_MODEL" "$BUNDLE_DIR/MODELS.txt" \
    || warn "chat model '$CHAT_MODEL' not listed in bundle MODELS.txt — the image may not boot with it"
fi
ok "model bundle ready ($(du -sh "$BUNDLE_DIR" | cut -f1))"

# ── 2. Build the image ────────────────────────────────────────────────────────
# The appliance needs deploy/ollama-bundle/ in the build context, which the
# default .dockerignore excludes. BuildKit reads Dockerfile.appliance.dockerignore
# automatically; the classic builder only reads the context-root .dockerignore, so
# there we swap in the appliance ignore for the duration of the build and ALWAYS
# restore it (trap on EXIT/INT/TERM) — the two files differ only by the bundle line.
_ROOT_IGNORE="$APP_DIR/.dockerignore"
_APP_IGNORE="$APP_DIR/Dockerfile.appliance.dockerignore"
_IGNORE_BACKUP=""
_IGNORE_CREATED=0
_restore_ignore() {
  if [ -n "$_IGNORE_BACKUP" ] && [ -f "$_IGNORE_BACKUP" ]; then
    mv -f "$_IGNORE_BACKUP" "$_ROOT_IGNORE"
  elif [ "$_IGNORE_CREATED" = 1 ]; then
    rm -f "$_ROOT_IGNORE"
  fi
  _IGNORE_BACKUP=""; _IGNORE_CREATED=0
}

_docker_build() {
  docker build \
    -f "$DOCKERFILE" \
    --build-arg "RAG_CHAT_MODEL=$CHAT_MODEL" \
    --build-arg "RAG_EMBED_MODEL=$EMBED_MODEL" \
    -t "$IMAGE_TAG" \
    "$APP_DIR"
}

say "Building $IMAGE_TAG from Dockerfile.appliance"
if docker buildx version >/dev/null 2>&1; then
  say "  builder: BuildKit (buildx) — reads Dockerfile.appliance.dockerignore"
  DOCKER_BUILDKIT=1 _docker_build || die "docker build failed"
else
  warn "buildx/BuildKit unavailable — using the classic builder (temporary .dockerignore swap)"
  [ -f "$_APP_IGNORE" ] || die "missing $_APP_IGNORE (needed for the classic-builder path)"
  if [ -f "$_ROOT_IGNORE" ]; then
    _IGNORE_BACKUP="$(mktemp "${_ROOT_IGNORE}.appliance-bak.XXXXXX")"
    cp -f "$_ROOT_IGNORE" "$_IGNORE_BACKUP"
  else
    _IGNORE_CREATED=1
  fi
  trap '_restore_ignore' EXIT INT TERM
  cp -f "$_APP_IGNORE" "$_ROOT_IGNORE"
  rc=0
  DOCKER_BUILDKIT=0 _docker_build || rc=$?
  _restore_ignore
  trap - EXIT INT TERM
  [ "$rc" -eq 0 ] || die "docker build failed"
fi
IMG_SIZE="$(docker image inspect "$IMAGE_TAG" --format '{{.Size}}' 2>/dev/null || echo 0)"
ok "built $IMAGE_TAG (uncompressed $(numfmt --to=iec "$IMG_SIZE" 2>/dev/null || echo "${IMG_SIZE} bytes"))"

# ── 3. Save → gzipped tarball for offline transport ───────────────────────────
if [ "$DO_SAVE" -eq 1 ]; then
  mkdir -p "$DIST_DIR"
  # pigz (parallel gzip) is much faster on the multi-GB model layers; fall back to gzip.
  if command -v pigz >/dev/null 2>&1; then GZIP_BIN=pigz; else GZIP_BIN=gzip; fi
  say "Saving → $TARBALL  (compressing with $GZIP_BIN; this is large; ~minutes)"
  docker save "$IMAGE_TAG" | "$GZIP_BIN" > "$TARBALL" || die "docker save failed"
  SIZE="$(du -h "$TARBALL" | cut -f1)"
  SHA="$(sha256sum "$TARBALL" | cut -d' ' -f1)"
  ok "wrote $TARBALL  ($SIZE)"
  echo "  sha256: $SHA"

  cat <<EOF

──────────────────────────────────────────────────────────────────────────────
 Deploy on any NVIDIA box (needs the driver + nvidia-container-toolkit):

   docker load < $(basename "$TARBALL")
   docker run -d --gpus all --name pdm-legal \\
     -p 127.0.0.1:8000:8000 \\
     -v \$HOME/law-data:/app/data \\
     $IMAGE_TAG

 Then open http://127.0.0.1:8000 and create the first (admin) user.
 Data (users, uploads, vector store, JWT secret) persists in \$HOME/law-data.
 LAN exposure is opt-in: publish -p 0.0.0.0:8000:8000 only behind TLS.
──────────────────────────────────────────────────────────────────────────────
EOF

  # ── 4. Optional: stream the image to a remote box over SSH (Tailscale) ──────
  if [ -n "$SSH_TO" ]; then
    say "Streaming image to $SSH_TO (docker load over ssh)"
    command -v ssh >/dev/null 2>&1 || die "ssh not found"
    gunzip -c "$TARBALL" | ssh "$SSH_TO" docker load \
      || die "remote docker load failed (is docker installed on $SSH_TO?)"
    ok "loaded $IMAGE_TAG on $SSH_TO — run it there with the command above"
  fi
else
  warn "skipped saving a tarball (--no-save); image is in the local Docker daemon as $IMAGE_TAG"
fi

say "DONE"
