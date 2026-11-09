# Local AI Law: the app

Local-first legal document intelligence, powered by Ollama. Documents, chat history, wiki
pages, annotations and indexes stay on the machine. No cloud APIs. No telemetry.

The repository README (two folders up) has the demo, the paper and the quick start.

## What It Does

- Upload legal documents into a client. **Files** is one flat list (no folders to manage;
  files already in folders on disk are listed by name and keep their paths). Each file shows
  a red cross until it is ready, its percentage while it is indexed, and a green tick once
  chat can use it (`IdxStatus.file_progress`: reading a file is 50, embedding its own pieces
  fills the rest; `GET /api/groups/{id}/files/tree` carries `progress` while indexing, and
  the list asks again every 2 seconds). A file that is not ready gets **Try again**.
- Index documents into a local ChromaDB vector store with BM25 reranking.
- Generate document summaries, wiki pages, concept graph data, and contradiction checks.
- Ask questions over retrieved wiki pages and source chunks with document/page citations.
- **Finder** (`/finder`) — ask "where do I look?" and get ranked document + section pointers with no
  generated answer (the lawyer reads the source; no synthesis, no hallucination). Reuses the
  matter-anchored retrieval (pulls the whole dossier for multi-doc questions) + cross-matter entity join.
- Tapping a file opens the reader: a PDF as a book, two pages side by side on a wide screen
  (one on a phone), turned with the arrows, the arrow keys or a swipe; Markdown and text as
  one long page (front matter shown as a details box), with Edit for .md and .txt. Annotations
  work as before.
- **Note** (in the header, on the dashboard and in the phone's top bar) opens a dialog: a
  title, the text and the client to keep it with (the open client by default). Saving writes
  `<title>.md` into that client through `POST /api/groups/{id}/files`, which starts an index
  run like an upload, so the note shows in Files and chat can use it. The same title twice
  becomes `<title> (2).md`, never an overwrite.
- Export group data as a ZIP archive, or download a formatted PDF matter report (overview + summaries).
- Manage users, backups and audit logs (admin). Settings shows a user only Profile,
  Language and Appearance; the chat has no model, Top-K or level controls.

Education features from the old study workspace were removed from this pure
legal edition. Old study, quiz, flashcard, progress, and study-bank URLs return
HTTP 410 for cached clients.

## Legal Trust Model

- Strict legal prompt: use only retrieved context for document-grounded answers.
- Every factual claim should cite document and page.
- Comparison answers use structured tables and keep categories/contracts separate.
- Missing information must be stated explicitly.
- No legal advice or outcome prediction.
- Privileged/sensitive sources can be marked and gated by role.
- Answers keep their evidence: each chat turn stores every cited passage word for word
  (rel-path, page, SHA-256, the file version indexed), capped at 4,000 characters a passage
  and 40,000 a turn, in the user's own `chat_history`. Opening a citation shows that stored
  passage and says whether the file still holds it (`kept`, `same`, `changed`, `gone`),
  after a reindex, an edit or a deletion: `GET /api/sessions/{id}/turns/{n}/passages/{k}`,
  a hash lookup in the user's own index, never a new retrieval (`services/citations.py`).
- Legal eval harness lives at `scripts/legal_eval.py`.
- A chat can be limited to some of a client's documents (the **Documents** button above
  the chat, all ticked by default). The chat sends `sources` (rel paths) with each query;
  `retrieve_carded` and `retrieve_smart` keep every pick inside them, and the wiki and the
  client overview stay out of a limited chat. **New chat**, in the same bar, starts over
  with all documents.
- **History** lists a client's chats under Today, Yesterday, Earlier this week, then by
  date; tapping one carries it on.

## Requirements

- Python 3.10+
- Ollama running locally
- Local models, for example:

```bash
ollama pull gemma3:12b
ollama pull qwen3-embedding:4b   # embeddings — 4b is the balanced pick; 8b = best quality, more VRAM
```

## Run

```bash
./run.sh install
./run.sh start
```

Manual:

```bash
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`.

## Verification

```bash
python -c "from app import app"
python scripts/security_smoke.py
python scripts/legal_eval.py
python scripts/legal_eval.py --full
```

`--full` needs a configured chat model and local Ollama access.

## Architecture

```text
app.py                  FastAPI entrypoint, auth/security middleware
config.py               Settings, path helpers, atomic JSON writes
models.py               Pydantic request models
routes/                 HTTP and WebSocket route handlers
services/               Indexing, retrieval, wiki, graph, auth, backup, Ollama
static/index.html       Main SPA shell
static/chat/            React chat island build output
static/vendor/          Bundled browser libraries
scripts/                Smoke tests, legal eval, backup helpers
```

Important invariants:

- Use config path helpers. Do not build user data paths manually.
- All model calls go through local Ollama helpers.
- Do not add external API calls.
- Use atomic write helpers for JSON/text state.
- Keep frontend settings, `DEFAULT_SETTINGS`, `SettingsIn`, and docs in sync.
- Validate relative document paths. Reject traversal and absolute paths.

## Technical settings (the owner's `.env`)

Every model, retrieval and generation setting is the owner's, set in `.env` (kept out of
git). The keys and their variables are the `TECH_ENV_KEYS` tuple in
`config.py`: one whose variable is set wins over the global and every user
`settings.json`, so no account changes it from the app. `GET /api/settings` returns a
user who is not an admin only `USER_SETTING_KEYS` (`app_language`, `edition`), and
`POST` from them changes only those; `/api/models`, `/api/models/rich` and
`/api/system/diagnostics` are admin only. The chat socket takes a per-query `model`,
`top_k` or `difficulty` only from an admin (the bench scripts). When the model can't
answer, the user reads "The assistant is not available right now" and the cause goes to
the log.

## Retrieval & graph settings

Set in `.env` (the variable names below):

- **Document Router** (`retrieval_router`, env `RAG_RETRIEVAL_ROUTER`, default on) — an LLM picks
  the relevant document(s) for the question, then answers from ≤5 focused chunks. Bounds:
  `router_max_docs` (3), `router_max_chunks` (5), `router_candidates` (12).
- **Reranker** (`reranker`, env `RAG_RERANKER`, default on; `reranker_model` falls back to the
  chat model) — one bounded LLM pass reorders candidate documents by relevance before routing.
  Fail-open: any error keeps the original order.
- **Auto-build Knowledge Graph** (`auto_graph_build`, env `RAG_AUTO_GRAPH_BUILD`, default on) —
  extracts concepts + relations into the graph during indexing so the Concept Map is populated
  automatically. The Concept Map shows an empty-state until a matter has been indexed.
- Context sent to the model is **token-budgeted**: system prompt + history + a fixed output
  reserve are subtracted from `num_ctx`, and the lowest-scored chunks are dropped first so
  generation is never truncated.

### Core settings

All Ollama-local. The models, `top_k`, `chunk_size`, `chunk_overlap`, `num_ctx`,
`temperature`, `top_p`, `repeat_penalty` and `difficulty` are set in `.env` as `RAG_<KEY>`
(`TECH_ENV_KEYS` has the exact names); the rest keep their defaults unless an admin posts
them to `/api/settings`. `app_language` is the one a user sets, in Settings.
Change together with `DEFAULT_SETTINGS` (config.py) and `SettingsIn` (models.py).

| Setting | Default | What it does |
|---|---|---|
| `chat_model` | `qwen3.5:9b` | Model that writes answers. |
| `embed_model` | `qwen3-embedding:4b` | Model that embeds chunks + queries. |
| `vision_model` | `` (off) | Optional model for image/scan pages. |
| `app_language` | `en` | UI + answer language (`en`/`nl`). |
| `top_k` | `10` | Chunks retrieved per query. |
| `chunk_size` | `400` | Tokens per chunk at indexing. |
| `chunk_overlap` | `60` | Token overlap between chunks. |
| `extraction_mode` | `markdown` | Document → text extraction mode. |
| `table_format` | `linearized` | How tables are fed to the model (`linearized` keeps rows intact). |
| `query_expansion` | `auto` | Bounded LLM query expansion (`off`/`auto`). |
| `retrieval_second_pass` | `true` | Re-query with the expanded query when results are weak. |
| `answer_templates` | `true` | Question-type-aware answer formatting. |
| `num_ctx` | `16384` | Context window (lower to `8192` for large local models). |
| `temperature` | `0.7` | Sampling temperature. |
| `top_p` | `0.9` | Nucleus sampling cutoff. |
| `repeat_penalty` | `1.1` | Repetition penalty. |
| `system_prompt` | edition default | Base legal answer policy prepended to every chat. |
| `difficulty` | `intermediate` | Answer detail level. |

## Data Layout

```text
data/
  instance.json
  edition.json
  jobs.db
  <username>/
    groups.json
    settings.json
    uploads/<group>/
    rag_db/
    knowledge.db
    wiki/<group>/
    doc_summaries/
    overviews/
    chat_history/
    annotations/
    backups/
```

## Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `RAG_DATA_DIR` | `./data` | Data root |
| `RAG_USERS_FILE` | `<code>/users.json` | Accounts store (containers point it at the data volume) |
| `RAG_AUDIT_LOG` | `<code>/audit.log` | Audit log path |
| `RAG_SETTINGS_FILE` | `<code>/settings.json` | Global admin settings (containers point it at the data volume) |
| `RAG_HOST` | `127.0.0.1` | Bind address (`./run.sh start --lan` for `0.0.0.0`) |
| `PDM_HOSTS` | empty | Names the box is reached by, comma separated (`law.firm.local`). Loopback, bare IPs and the machine's own hostname always work; any other Host gets a 400 (services/guard.py) |
| `RAG_PORT` | `8000` | Port |
| `RAG_MAX_UPLOAD_MB` | `50` | Upload limit |
| `RAG_JWT_SECRET` | auto-generated | JWT signing secret |
| `RAG_CRITICAL_WORKERS` | `1` | User-facing job pool |
| `RAG_LOW_WORKERS` | `1` | Background job pool |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama endpoint |
| `RAG_NUM_CTX` | `8192` | Default context window |
| `RAG_EMBED_BATCH_SIZE` | `32` | Embedding batch size |
