# routes/ — HTTP + WebSocket handlers

FastAPI routers, registered in `app.py`. Handlers are **thin**: validate input (Pydantic models in
`models.py`), check auth (every `/api/` request is auth-gated by the middleware in `app.py`), then
delegate to `services/`. New route file → register it in `app.py`.

| File | Endpoints |
|---|---|
| `chat.py` | `/ws/chat` WebSocket — retrieval → streamed grounded answer → sources; `/api/chat/token-estimate`. |
| `finder.py` | The **finder** ("where do I look?"): `GET /finder` page + `POST /api/find` — retrieval-only ranked doc+section pointers, **no LLM** (no synthesis, no hallucination). Per-user isolated like every group endpoint. |
| `documents.py` | Upload/delete/list/preview/serve documents; summaries; annotations; file + folder CRUD incl. recursive folder delete (traversal-guarded). |
| `groups.py` | Group (matter) CRUD + `GET /groups/{id}/report` (matter report download: HTML, or PDF with the optional WeasyPrint extra). |
| `indexing.py` | Trigger a (re)index and poll its status. |
| `jobs.py` | Job status + the ingestion pipeline. |
| `wiki.py` | Wiki browse + generation. |
| `graph.py` | Knowledge-graph read/write endpoints. |
| `sessions.py` | Chat history / saved sessions. |
| `settings.py` | Per-user settings + profile. |
| `setup.py` | First-launch setup wizard. |
| `auth.py` | Register/login/me + the shared `get_current_username` dependency. |
| `admin.py` | Admin-only: audit log, backup/restore (409 while jobs run), metrics, user management. |
| `license.py` | License management (admin only). |
| `export.py` | Stream a ZIP export of everything in a group. |
| `meta.py` | Health check (`/api/health`), models list, branding, edition. |
| `deprecated.py` | Removed education endpoints — return HTTP 410 for old cached clients. |
