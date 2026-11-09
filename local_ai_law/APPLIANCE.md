# pdm.ai Legal — self-contained appliance image

One Docker image with **everything baked in**: the app, the Ollama server, and the model
weights. Move the tarball to any NVIDIA box, `docker load`, `docker run --gpus all` — no
`ollama pull`, no internet, no `docker compose`. The only host requirement is the NVIDIA
driver + `nvidia-container-toolkit`.

This is separate from the lean dev `Dockerfile` (app only, talks to an external Ollama) and
`docker-compose.yml`. Use the appliance for **shipping a box to a client**.

---

## 1. Build (on a box that already has the models pulled)

The build box needs: Docker, internet **once** (base image + pip wheels), disk (~30 GB
scratch), and the three models pulled locally so they can be bundled:

```bash
ollama pull qwen3.5:9b          # RAG_CHAT_MODEL
ollama pull qwen3-embedding:4b  # RAG_EMBED_MODEL
ollama pull gemma4:e2b          # RAG_GRAPH_MODEL (concept map)

scripts/build_appliance.sh
```

That single command:
1. bundles those models into `deploy/ollama-bundle/` (native Ollama manifests+blobs),
2. builds `Dockerfile.appliance` → tags `pdm-legal:appliance`,
3. `docker save | gzip` → `dist/pdm-legal-appliance.tar.gz`,
4. prints the size, `sha256`, and the exact deploy command.

Useful flags: `--skip-bundle` (reuse an existing bundle), `--no-save` (build only),
`--tag pdm-legal:v1`, `--to <ssh-host>` (stream the image straight to a remote box over
SSH/Tailscale). Override the model set with `RAG_CHAT_MODEL` / `RAG_EMBED_MODEL` /
`RAG_GRAPH_MODEL` env vars.

The build uses BuildKit when `docker buildx` is present, and automatically falls back to the
classic builder otherwise (it temporarily swaps in the appliance's `.dockerignore` and always
restores it). The finished image is ~21 GB (base + Python + the three models baked once);
the models are stored a single time under `/models` — the build is written so `COPY` never
duplicates the multi-GB weights. Only the app **runtime** is copied in (`app.py`, `routes/`,
`services/`, `static/`, plus `config`/`models`/`gunicorn.conf`); the frontend Vite source,
the test suite, and the internal `scripts/` tooling (build/verify/bench/eval) are **not** in
the client image.

---

## 2. Deploy (on the target NVIDIA box)

Prerequisite — install the NVIDIA container toolkit once so `--gpus` works:

```bash
# Ubuntu/Debian (see NVIDIA docs for other distros)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```

Then load and run:

```bash
docker load < pdm-legal-appliance.tar.gz

docker run -d --gpus all --name pdm-legal \
  -p 127.0.0.1:8000:8000 \
  -v $HOME/law-data:/app/data \
  pdm-legal:appliance
```

Open <http://127.0.0.1:8000> and create the first user (becomes admin).

---

## 3. Per-machine configuration (env)

All optional — sensible defaults ship in the image. Pass with `-e VAR=value` on `docker run`.

| Var | Default | Purpose |
|-----|---------|---------|
| `RAG_JWT_SECRET` | auto-generated once into `/app/data/.jwt_secret` | session signing key; set explicitly to share sessions across redeploys |
| `RAG_CHAT_MODEL` | `qwen3.5:9b` | must be one of the baked models |
| `RAG_CHAT_MODEL_NL`, `RAG_CHAT_MODEL_EN` | empty | the chat model for answers in that language (the group's, or the app language); empty uses `RAG_CHAT_MODEL` (`services/lang.py`) |
| `RAG_EMBED_MODEL` | `qwen3-embedding:4b` | must be one of the baked models |
| `RAG_PORT` | `8000` | app port inside the container |
| `PDM_HOSTS` | empty | names the firm types to reach the box, comma separated (`law.firm.local`). Loopback and a bare IP always work; any other name gets a 400 |
| `RAG_SETTINGS_FILE` | `/app/data/settings.json` | global admin settings on the volume (pre-set; override only to relocate) |
| `RAG_USERS_FILE` | `/app/data/users.json` | accounts store on the volume (pre-set) |

**Data persistence:** everything mutable lives on the `/app/data` volume — user accounts
(`users.json`), the audit log, global settings (`settings.json`), the JWT secret, uploads,
the per-user vector stores, and backups. The users/audit/settings paths are pinned there by
the `RAG_USERS_FILE` / `RAG_AUDIT_LOG` / `RAG_SETTINGS_FILE` env vars baked into the image. The
`-v $HOME/law-data:/app/data` bind mount is what makes the appliance stateful across
`docker rm` / image upgrades. Back it up; it is the whole workspace. (The app code itself
lives at `/app/local_ai_law` inside the image and is read-only at runtime.) The
volume root is 0700 and the entrypoint sets `umask 077`, so every file and folder on it is
owner-only.

**Backup and restore.** Two layers:

- *In the app* (Admin > Backup): one account's data, kept on the same volume (0600, newest
  `RAG_BACKUP_KEEP_COUNT` kept). Good for undoing a mistake, useless if the disk dies.
- *The whole box*, from the host: `scripts/backup.sh backup /mnt/offsite` stops the
  container, copies the whole volume (all accounts, `users.json`, settings, JWT secret,
  audit log; per-user backups left out) to a 0600 archive plus a `.sha256`, and starts it
  again. `scripts/backup.sh restore <file>` checks every member (no absolute path, `..`,
  link or device), unpacks into staging, swaps it in, and keeps the old data until
  `/api/health` answers on the restored data; otherwise it puts the old data back. Runs
  inside the image as `appuser`, so no sudo. Set `PDM_CONTAINER` / `PDM_DATA` if you
  renamed the container or volume. Keep the archive on an encrypted disk off the box: it
  is every document the firm uploaded.

Tested 2026-10-06 on the appliance image (weights left out): in-app backup and restore,
box backup, a wiped account and `users.json` restored with logins and tokens intact, and
an archive holding a symlink refused with the data untouched, and nothing on the volume
readable by group or others afterwards.

---

## 4. Security posture

- **Non-root** — the web process runs as an unprivileged `appuser`; the model store is
  read-only to it.
- **No baked secrets** — `.dockerignore` / `Dockerfile.appliance.dockerignore` exclude
  `.jwt_secret`, `users.json`, `audit.log`, `certs/`, `data/`, `*.db`. The JWT secret is
  generated at first run into the data volume (0600), never into the image.
- **Loopback by default** — the run command publishes to `127.0.0.1` only. LAN exposure is
  opt-in and should sit behind TLS (`-p 0.0.0.0:8000:8000` only with a reverse proxy +
  certificate; see `nginx.conf`).
- **Front door** (`services/guard.py`, outermost middleware, HTTP and WebSocket): a path
  that walks (`..`, `//`, `%2f`, backslash, double encoding) gets 400, a Host that is not
  this box gets 400 (stops DNS rebinding while the first-run setup is open), and a change or
  WebSocket from another site's page gets 403. Reach the box by a name? Put it in `PDM_HOSTS`.

---

## 5. Verify

```bash
scripts/verify.sh                                  # the gates: selfcheck, security_smoke, pytest, ruff, repo guards, frontend
scripts/verify.sh --image pdm-legal:appliance      # + boot the image, assert /api/health + baked /api/tags
scripts/verify.sh --live                           # + retrieval-quality eval (needs Ollama + a seeded index)
```

`verify.sh` runs every check and ends with a table: PASS, FAIL or SKIP, each marked `mocked`
(test doubles, says the code is consistent) or `real models` (`--live`, `--image`). It exits
non-zero if any check failed, so it gates a build; a SKIP prints its reason and never fails
the run. The frontend build goes to a temp folder and must equal `static/chat`, so a source
change without `./run.sh build-chat` fails. The image smoke
runs CPU-only by default (listing baked models needs no GPU); set `SMOKE_GPUS=all` to boot it
on the GPU.
