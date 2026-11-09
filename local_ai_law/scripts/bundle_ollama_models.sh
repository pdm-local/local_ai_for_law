#!/usr/bin/env bash
#
# bundle_ollama_models.sh — pack the models an appliance needs into a self-contained
# payload so a deploy box needs NO `ollama pull` and NO internet ("model already packed").
#
# Ollama stores every model as a manifest (JSON) + content-addressed blobs. This copies just
# the manifests and blobs for the requested models into deploy/ollama-bundle/, deduping shared
# layers. provision.sh on the target imports that payload into the target's Ollama store.
#
# Run on the BUILD box (the one that already pulled the models). Unprivileged: Ollama blobs
# are world-readable, so this only READS the store.
#
#   scripts/bundle_ollama_models.sh                         # default model set
#   scripts/bundle_ollama_models.sh gemma4:e4b qwen3-embedding:4b
#   RAG_MODELS="a:1 b:2" scripts/bundle_ollama_models.sh    # or via env
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="$APP_DIR/venv/bin/python"; [ -x "$PY" ] || PY="python3"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Models to bundle (chat + embed mandatory; graph powers the concept map). Judge is
# benchmark-only — never bundled into a deploy box. Keep in sync with provision.sh RAG_MODELS.
MODELS=("$@")
if [ ${#MODELS[@]} -eq 0 ]; then
  # shellcheck disable=SC2206
  MODELS=(${RAG_MODELS:-gemma4:e4b qwen3-embedding:4b gemma4:e2b})
fi

# Locate the source Ollama store (where models were pulled).
detect_root() {
  if [ -n "${OLLAMA_MODELS:-}" ]; then echo "$OLLAMA_MODELS"; return; fi
  local env_line
  env_line="$(systemctl show ollama -p Environment 2>/dev/null | tr ' ' '\n' | sed -n 's/^OLLAMA_MODELS=//p' | head -1)"
  if [ -n "$env_line" ]; then echo "$env_line"; return; fi
  for c in /usr/share/ollama/.ollama/models "$HOME/.ollama/models"; do
    [ -d "$c/manifests" ] && { echo "$c"; return; }
  done
  echo "/usr/share/ollama/.ollama/models"
}

SRC="$(detect_root)"
OUT="$APP_DIR/deploy/ollama-bundle"
[ -d "$SRC/manifests" ] || die "no Ollama store at $SRC (set OLLAMA_MODELS). Nothing to bundle."
say "Source Ollama store: $SRC"
say "Bundle output:       $OUT"
mkdir -p "$OUT/blobs"

manifest_path() {  # name:tag -> manifest file under $SRC, globbing across registry hosts
  local ref="$1" name tag
  name="${ref%%:*}"; tag="${ref##*:}"; [ "$tag" = "$ref" ] && tag="latest"
  find "$SRC/manifests" -path "*/$name/$tag" -type f 2>/dev/null | head -1
}

total=0
for ref in "${MODELS[@]}"; do
  mf="$(manifest_path "$ref")"
  [ -n "$mf" ] || die "manifest for '$ref' not found under $SRC (is it pulled? 'ollama pull $ref')"
  # Copy the manifest preserving its registry/namespace path so Ollama recognises it.
  rel="${mf#"$SRC"/}"
  mkdir -p "$OUT/$(dirname "$rel")"
  cp -f "$mf" "$OUT/$rel"
  # Pull every blob digest referenced by this manifest (config + all layers).
  digests="$("$PY" - "$mf" <<'PY'
import json,sys
m=json.load(open(sys.argv[1]))
ds=[m.get("config",{}).get("digest")]
ds+=[l.get("digest") for l in m.get("layers",[])]
print("\n".join(d for d in ds if d))
PY
)"
  n=0
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    blob="blobs/${d/:/-}"
    if [ ! -f "$OUT/$blob" ]; then
      [ -f "$SRC/$blob" ] || die "blob missing for $ref: $SRC/$blob"
      cp -f "$SRC/$blob" "$OUT/$blob"
    fi
    n=$((n+1))
  done <<< "$digests"
  ok "bundled $ref ($n layers)"
  total=$((total+1))
done

# Record what's in the bundle (provision.sh reads this to verify after import).
printf '%s\n' "${MODELS[@]}" > "$OUT/MODELS.txt"
BYTES="$(du -sb "$OUT" | cut -f1)"
ok "Bundled $total model(s) → $OUT  ($(du -sh "$OUT" | cut -f1))"
echo
echo "Next: ship the appliance (deploy/ollama-bundle is imported by provision.sh on the target,"
echo "so the deploy box needs no 'ollama pull' and no internet)."
