#!/usr/bin/env bash
# backup.sh - whole-box backup and restore for the pdm.ai Legal appliance (run on the host).
#
#   scripts/backup.sh backup [out_dir]       # default out_dir: ./law-backups
#   scripts/backup.sh restore <file.tar.gz>
#
# Env: PDM_CONTAINER (default pdm-legal), PDM_DATA (default $HOME/law-data: the host folder
# or volume name mounted at /app/data), PDM_IMAGE (default: the container's image).
#
# The in-app backup (Admin > Backup) is per user and sits on the same disk as the data.
# This one copies the whole data volume: every account, users.json, settings, the JWT
# secret and the audit log, so a dead disk or a new box can be brought back.
# - The app is stopped while it copies: SQLite and Chroma files are only consistent at rest.
# - Tar and extraction run inside the appliance image as the app user, so owners stay
#   right and no sudo is needed.
# - The archive is 0600 in a 0700 folder: it holds every document the firm uploaded.
#   Keep the copy off the box, on an encrypted disk.
# - Restore checks every member (no absolute path, no "..", only files and folders),
#   extracts into a staging folder inside the volume, swaps it in, starts the app and
#   waits for /api/health. Only then is the old data deleted; a failure at any step puts
#   the old data back and starts the app on it.
set -euo pipefail

CONTAINER="${PDM_CONTAINER:-pdm-legal}"
DATA="${PDM_DATA:-$HOME/law-data}"

die() { echo "backup.sh: $*" >&2; exit 1; }
command -v docker >/dev/null || die "docker not found"
docker inspect "$CONTAINER" >/dev/null 2>&1 || die "no container named $CONTAINER (set PDM_CONTAINER)"
IMAGE="${PDM_IMAGE:-$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")}"
was_running="$(docker inspect -f '{{.State.Running}}' "$CONTAINER")"

# One-off helper in the appliance image with the data volume at /data.
helper() { docker run --rm -i --network none --entrypoint "$1" -v "$DATA:/data" "$IMAGE" "${@:2}"; }

# Restore steps, run by the image's python as the app user. Staging and the old copy
# live inside the volume so every move is a rename on one filesystem.
read -r -d '' PY <<'EOF' || true
import os, shutil, sys, tarfile
os.umask(0o077)  # the "data" filter makes folders with the umask; a firm's files stay owner-only
D = "/data"
ST, OLD = os.path.join(D, ".box-restore-staging"), os.path.join(D, ".box-restore-old")
KEEP = {".box-restore-staging", ".box-restore-old"}

def move_all(src, dst):
    for n in os.listdir(src):
        if n not in KEEP:
            os.replace(os.path.join(src, n), os.path.join(dst, n))

def clear(path):
    for n in os.listdir(path):
        if n not in KEEP:
            p = os.path.join(path, n)
            shutil.rmtree(p) if os.path.isdir(p) and not os.path.islink(p) else os.unlink(p)

step = sys.argv[1]
if step == "extract":
    shutil.rmtree(ST, ignore_errors=True)
    os.mkdir(ST, 0o700)
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as tar:
            for m in tar:
                parts = m.name.split("/")
                if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                    raise SystemExit(f"refused archive member: {m.name!r}")
                tar.extract(m, ST, filter="data")
        if not os.path.isfile(os.path.join(ST, "users.json")):
            raise SystemExit("not a box backup: no users.json at the top")
    except BaseException:
        shutil.rmtree(ST, ignore_errors=True)
        raise
elif step == "swap":
    shutil.rmtree(OLD, ignore_errors=True)
    os.mkdir(OLD, 0o700)
    try:
        move_all(D, OLD)
        move_all(ST, D)
    except BaseException:
        clear(D)
        move_all(OLD, D)
        raise
    finally:
        shutil.rmtree(ST, ignore_errors=True)
elif step == "rollback":
    if os.path.isdir(OLD):
        clear(D)
        move_all(OLD, D)
        shutil.rmtree(OLD)
elif step == "commit":
    shutil.rmtree(OLD, ignore_errors=True)
EOF

wait_healthy() {
  for _ in $(seq 1 120); do
    docker exec "$CONTAINER" curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1 && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" = true ] || return 1
    sleep 1
  done
  return 1
}

backup() {
  out="${1:-./law-backups}"
  umask 077
  mkdir -p "$out"; chmod 700 "$out"
  file="$out/law-box-$(date +%Y-%m-%d_%H-%M-%S).tar.gz"
  [ "$was_running" = true ] && { echo "stopping $CONTAINER"; docker stop "$CONTAINER" >/dev/null; }
  trap 'rm -f "$file.partial"; if [ "$was_running" = true ]; then docker start "$CONTAINER" >/dev/null; fi' EXIT
  # Per-user backups are left out (they are older copies of the same data), and so is
  # restore scratch space.
  helper tar -C /data -czf - --exclude='./*/backups' --exclude='./*/.restore_*' \
    --exclude='./.box-restore-*' . > "$file.partial"
  mv "$file.partial" "$file"
  sha256sum "$file" > "$file.sha256"
  echo "wrote $file ($(du -h "$file" | cut -f1))"
}

restore() {
  local file="${1:-}"
  [ -f "$file" ] || die "usage: backup.sh restore <file.tar.gz>"
  [ ! -f "$file.sha256" ] || sha256sum -c --quiet "$file.sha256" || die "checksum does not match $file.sha256"
  echo "checking and unpacking $file"
  helper python3 -I -c "$PY" extract < "$file" || die "archive refused, current data untouched"
  [ "$was_running" = true ] && { echo "stopping $CONTAINER"; docker stop "$CONTAINER" >/dev/null; }
  if ! helper python3 -I -c "$PY" swap; then
    [ "$was_running" = true ] && docker start "$CONTAINER" >/dev/null
    die "swap failed, old data put back"
  fi
  docker start "$CONTAINER" >/dev/null
  if wait_healthy; then
    helper python3 -I -c "$PY" commit
    echo "restored $file; $CONTAINER is healthy"
  else
    echo "app did not come up on the restored data, putting the old data back" >&2
    docker stop "$CONTAINER" >/dev/null
    helper python3 -I -c "$PY" rollback
    docker start "$CONTAINER" >/dev/null
    die "restore rolled back"
  fi
  [ "$was_running" = true ] || docker stop "$CONTAINER" >/dev/null
}

case "${1:-}" in
  backup)  backup "${2:-}" ;;
  restore) restore "${2:-}" ;;
  *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
