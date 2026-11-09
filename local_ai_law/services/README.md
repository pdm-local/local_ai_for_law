# services/ — business logic

All app logic lives here; `routes/` handlers stay thin and call into these modules. Ollama-only
(never an external API); a local cross-encoder is allowed. Always pass `username` to data-path
helpers and load models from settings (never hardcode) — see the repo `CLAUDE.md` invariants.

## Retrieval & indexing
| File | Purpose |
|---|---|
| `retrieval.py` | Wiki-first hybrid retrieval pipeline (entry `retrieve_smart`; card path `retrieve_carded`). Multi-doc questions widen doc SELECTION before fetching chunks — `_matter_of`/`_matter_siblings` matter-anchor (pull the whole dossier) + entity-join, then a per-doc chunk floor (`_retrieve_picked`); `_keyword_expand` is the last-resort fallback. `find_pointers` is the LLM-free **finder** path (aggregates routed chunks → ranked doc+section pointers; see `routes/finder.py`). |
| `entity_index.py` | Deterministic frontmatter-derived inverted index (party/statute/matter → docs) for cross-matter connection joins; zero-NER query-side substring match; hub guard by matter-span. Built by `scripts/build_entity_index.py` + the `do_index_group` hook. |
| `doc_cards.py` | Per-document keyword-card index (RRF hybrid card search) for document-level retrieval. |
| `doc_summary.py` | Per-document summary/card extraction (the cards retrieval depends on; robust JSON parse + model fallback). Stores `matter` + `statutes` from frontmatter so cards carry the matter-anchor join key. |
| `doc_router.py` | Picks the most relevant document(s) for a query (LLM router — legacy selection path). |
| `cross_encoder.py` | **Optional** local cross-encoder reranker (bge-reranker-v2-m3), GPU with CPU-OOM fallback. Needs `sentence_transformers`+`torch` (NOT in requirements.txt); `available()` gates it, so it stays OFF unless those deps are installed. Retrieval fails open to hybrid ordering + the Ollama reranker when absent. |
| `reranker.py` | Bounded LLM-as-judge rerank pass (Ollama) that reorders candidates — the default rerank path, needs no extra deps. |
| `query_expansion.py` | Adds related terminology to a query before search (bounded, fail-open). |
| `embedding.py` | Ollama embedding wrapper (query-instruct prefix), 100% local. |
| `indexer.py` | Background per-group indexing: extract → chunk → embed → cards → graph. |
| `chunking.py` | Split extracted text into chunks (semantic + word-window) and content-hash chunk IDs. |
| `extraction.py` | Text extraction for every supported file type (PDF/DOCX/CSV/XLSX/HTML/…). |
| `normalizer.py` | Raw extraction output → canonical markdown artifact. |

## Knowledge graph & wiki
| File | Purpose |
|---|---|
| `graph.py` | Local knowledge graph: SQLite nodes/edges + FTS5 wiki index (one DB per user). |
| `wiki.py` | Generate/store/manage wiki pages; keyword-graph builder for retrieval expansion. |
| `contradictions.py` | Cross-document contradiction detection (conflicting claims across a matter). |
| `overview.py` | Matter overview: summary, topics, per-document key ideas + a wiki page. |
| `reports.py` | Auto-generated matter reports as escaped HTML; PDF only with the optional WeasyPrint extra (`requirements-pdf.txt`). |

## Answering
| File | Purpose |
|---|---|
| `prompting.py` | Shared system-prompt assembly (chat + legal_eval). |
| `answer_templates.py` | Question-type detection + structured answer-format prompts. |
| `vision.py` | Local Ollama multimodal pipeline (sequential image analysis). |
| `ollama_client.py` | Shared Ollama client + the thread-safe single-GPU concurrency gate. |

## Platform
| File | Purpose |
|---|---|
| `auth.py` | Authentication: JWT + Argon2id passwords, brute-force lockout, 100% local. |
| `jobs.py` | Background job orchestration (CRITICAL vs LOW pools). |
| `scheduler.py` | User registry helper. |
| `annotations.py` | Per-user, per-document highlights/notes. |
| `editions.py` | Law-firm edition + module registry (branding/feature gating). |
| `license.py` | Ed25519 offline license verification + instance lockout. |
| `backup.py` | Per-user tarball snapshot/restore of all data: owner-only file, SQLite snapshotted via its backup API, restore swaps with rollback, refuses (409) while jobs run, drops cached DB/Chroma handles. |
| `audit.py` | Structured JSON-lines audit trail. |
| `metrics.py` | System metrics for the health dashboard. |
