#!/usr/bin/env bash
#
# verify.sh — ONE gate for the pdm.ai Legal appliance. Runs every check, then prints a
# table: PASS / FAIL / SKIP, and whether each one ran on test doubles ("mocked") or on
# the real models ("real models"). A green table means "safe to build / ship".
#
#   scripts/verify.sh                      # the gates: offline, no GPU, no network
#   scripts/verify.sh --live               # + retrieval-quality eval (needs Ollama + an index)
#   scripts/verify.sh --image TAG          # + boot the built image, prove app health + baked models
#   scripts/verify.sh --image TAG --live   # everything
#
# The gates (always, mocked: they say the code is consistent, not that answers are good):
#   selfcheck.py --strict   whole-tree health audit (WARN = failure)
#   security_smoke.py       auth / path / front door / backup / contract smoke
#   pytest tests/           auth, isolation, refusal, upload-validation, hardening, guard
#   ruff                    lint
#   frontend type check     tsc --noEmit                         (SKIP without node_modules)
#   frontend build          vite build into a temp dir, must equal static/chat byte for byte
# Opt-in (real models):
#   --live       legal_eval.py            retrieval metrics (needs the embed model + a seeded index)
#   --image TAG  post-build image smoke   docker run, wait /api/health, confirm baked /api/tags, tear down
#
# Every check runs even after one fails, so the table shows all of them. Exits non-zero if
# any FAILED, so it still gates build_appliance.sh. A SKIP never fails the run; its reason
# is printed, and a report must say which kind of check it is quoting.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$APP_DIR/venv/bin/python"; [ -x "$PY" ] || PY="python3"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 2; }

LIVE=0
IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --live)  LIVE=1; shift ;;
    --image) IMAGE="${2:-}"; [ -n "$IMAGE" ] || die "--image needs a tag (e.g. pdm-legal:appliance)"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown arg: $1 (see --help)" ;;
  esac
done

cd "$APP_DIR"

FAILED=0
LINES=()

# run KIND NAME CMD...  — run one check and record PASS or FAIL.
run() {
  local kind="$1" name="$2"; shift 2
  say "$name  ($kind)"
  if "$@"; then
    ok "$name"; LINES+=("PASS|$kind|$name")
  else
    printf '\033[1;31m✗ %s\033[0m\n' "$name"; LINES+=("FAIL|$kind|$name"); FAILED=$((FAILED + 1))
  fi
}

# skip KIND NAME REASON
skip() { say "$2  ($1)"; warn "skipped: $3"; LINES+=("SKIP|$1|$2: $3"); }

# ── THE GATES (always; seconds, no GPU, no network) ───────────────────────────
run mocked "selfcheck --strict" "$PY" scripts/selfcheck.py --strict
run mocked "security smoke"     "$PY" scripts/security_smoke.py
run mocked "pytest"             "$PY" -m pytest tests/ -q
if [ -x venv/bin/ruff ] || command -v ruff >/dev/null 2>&1; then
  RUFF="venv/bin/ruff"; [ -x "$RUFF" ] || RUFF="ruff"
  run mocked "ruff" "$RUFF" check .
else
  skip mocked "ruff" "not installed; ./run.sh install"
fi

# Frontend. The build goes to a temp copy outside the tree: it never rewrites static/chat,
# and a stray package.json in a parent folder (esbuild reads them) cannot break it. The
# result must equal the committed static/chat, so a source change without a rebuild fails.
frontend_build() {
  local tmp rc=0
  tmp="$(mktemp -d)" || return 1
  cp -r frontend/index.html frontend/package.json frontend/tsconfig.json frontend/vite.config.ts \
        frontend/src "$tmp/" && ln -s "$APP_DIR/frontend/node_modules" "$tmp/node_modules" \
    && (cd "$tmp" && ./node_modules/.bin/vite build --outDir "$tmp/out" --emptyOutDir --logLevel warn) \
    && diff -r "$tmp/out" static/chat || rc=1
  [ "$rc" -eq 0 ] || warn "static/chat differs from a fresh build: run ./run.sh build-chat and commit it"
  rm -rf "$tmp"
  return "$rc"
}
if ! command -v node >/dev/null 2>&1; then
  skip mocked "frontend type check" "no node"
  skip mocked "frontend build"      "no node"
elif [ ! -x frontend/node_modules/.bin/tsc ]; then
  skip mocked "frontend type check" "no frontend/node_modules; (cd frontend && npm ci)"
  skip mocked "frontend build"      "no frontend/node_modules; (cd frontend && npm ci)"
else
  run mocked "frontend type check" bash -c "cd frontend && ./node_modules/.bin/tsc --noEmit"
  run mocked "frontend build"      frontend_build
fi

# ── LIVE RETRIEVAL EVAL (opt-in; needs Ollama up + a seeded index; uses the GPU) ──
if [ "$LIVE" -eq 1 ]; then
  run "real models" "legal_eval" "$PY" scripts/legal_eval.py
else
  skip "real models" "legal_eval" "add --live (Ollama up, seeded index, no other GPU job)"
fi

# ── POST-BUILD IMAGE SMOKE (opt-in; needs Docker + the built image) ────────────
image_smoke() {
  command -v docker >/dev/null 2>&1 || { warn "docker not found"; return 1; }
  docker image inspect "$IMAGE" >/dev/null 2>&1 || { warn "image '$IMAGE' not found (build it first)"; return 1; }

  local HOSTPORT="${SMOKE_PORT:-18080}" CID healthy=0 TAGS MODEL_COUNT
  local GPUS_ARG=()
  [ -n "${SMOKE_GPUS:-}" ] && GPUS_ARG=(--gpus "$SMOKE_GPUS")

  echo "booting $IMAGE  (host 127.0.0.1:${HOSTPORT} → app :8000)"
  CID="$(docker run -d --rm -p "127.0.0.1:${HOSTPORT}:8000" "${GPUS_ARG[@]}" "$IMAGE")" \
    || { warn "docker run failed"; return 1; }
  # Always tear the container down, even on failure below.
  trap 'docker stop "$CID" >/dev/null 2>&1 || true; trap - RETURN' RETURN

  # App health: the entrypoint starts Ollama, waits for it, then gunicorn.
  echo "waiting for app health on /api/health (up to 150s)"
  for _ in $(seq 1 150); do
    if curl -fsS "http://127.0.0.1:${HOSTPORT}/api/health" >/dev/null 2>&1; then
      healthy=1; break
    fi
    if ! docker ps -q --no-trunc | grep -q "$CID"; then
      echo "--- container exited early; last logs: ---" >&2
      docker logs "$CID" 2>&1 | tail -30 >&2 || true
      warn "container exited before becoming healthy"; return 1
    fi
    sleep 1
  done
  [ "$healthy" -eq 1 ] || { docker logs "$CID" 2>&1 | tail -30 >&2 || true; warn "app never became healthy"; return 1; }
  ok "app is healthy"

  # Baked models: Ollama is in-container on 127.0.0.1:11434 (not published).
  TAGS="$(docker exec "$CID" curl -fsS http://127.0.0.1:11434/api/tags 2>/dev/null || true)"
  [ -n "$TAGS" ] || { warn "could not read /api/tags inside the container"; return 1; }
  MODEL_COUNT="$(printf '%s' "$TAGS" | "$PY" -c 'import sys,json; print(len(json.load(sys.stdin).get("models",[])))' 2>/dev/null || echo 0)"
  [ "${MODEL_COUNT:-0}" -ge 1 ] || { echo "$TAGS" >&2; warn "no models baked into the image"; return 1; }
  ok "image ships ${MODEL_COUNT} baked model(s)"
  # If a chat model is named, insist it is actually present.
  if [ -n "${RAG_CHAT_MODEL:-}" ]; then
    printf '%s' "$TAGS" | grep -q "${RAG_CHAT_MODEL%%:*}" \
      || { warn "expected chat model '${RAG_CHAT_MODEL}' not found in /api/tags"; return 1; }
    ok "chat model '${RAG_CHAT_MODEL}' present"
  fi
}
if [ -n "$IMAGE" ]; then
  run "real models" "image smoke $IMAGE" image_smoke
else
  skip "real models" "image smoke" "add --image TAG after scripts/build_appliance.sh"
fi

# ── SUMMARY ────────────────────────────────────────────────────────────────────
printf '\n===== summary =====\n'
for line in "${LINES[@]}"; do
  IFS='|' read -r state kind name <<<"$line"
  printf '%-4s  %-11s  %s\n' "$state" "$kind" "$name"
done
if [ "$FAILED" -eq 0 ]; then
  printf '\n\033[1;32mALL CHECKS PASSED\033[0m (everything that ran; SKIP lines did not run)\n'
else
  printf '\n\033[1;31m%s check(s) FAILED\033[0m\n' "$FAILED"
fi
exit $((FAILED > 0))
