# scripts/ — CLI tools, evaluation & benchmark harnesses

Run with the venv python and `PYTHONPATH=<repository root>` (the folder that holds `local_ai_law/`). Anything that touches a model is
GPU-serial — run long jobs in `tmux`. The bilingual benchmark + corpus design lives in
`../../legal_corpus` (read its `SELFHEAL_README.md` / `SELFHEAL_RUNBOOK.md` and the skills first).

**Health audit (start here):** `python scripts/selfcheck.py` — one offline, GPU-free command that
audits the whole tree (compile + import, docstring/lint sweep, settings triangle, every endpoint
auth-gated, contract smokes, **and a UI section**: every `static/index.html` on*-handler is defined,
every `/api` the UI calls maps to a real route, orphaned functions flagged — cross-bundle aware for the
React chat) and points at problems. App-touching sections are timeboxed (`--timeout`, default 45s) so it
never hangs. `--strict` makes WARN fail (CI gate); `--json` for machine output; `--self-test` proves the
endpoint auditor catches a planted bug.

## Benchmarks & evaluation
| File | Purpose |
|---|---|
| `selfheal_bench.py` | The bilingual (NL+EN) self-heal benchmark — Family A (deterministic) + Family B (LLM-judge). |
| `legal_eval.py` | Fixed-question RAG quality harness (`--full` adds answer scoring). |
| `test_refusal_gate.py` | Unit guard against `_REFUSAL` regex regressions (refusals score, fabrications don't). |
| `security_smoke.py` | Lightweight security + contract smoke checks (imports, auth, public endpoints, Ollama). |
| `selfcheck.py` | **Whole-tree health audit** (offline, GPU-free): compile/import, docstring+lint sweep, settings triangle, every endpoint auth-gated + no 5xx, and the `security_smoke` contracts — folded behind one command with a FAIL/WARN exit code. |

## Indexing / corpus maintenance
| File | Purpose |
|---|---|
| `backfill_matter.py` | No-LLM pass that writes `matter` (+ `statutes`) from frontmatter into existing `doc_summaries/<group>.json` — enables matter-anchored retrieval on already-built groups without a card rebuild. |
| `build_entity_index.py` | No-LLM build of the per-group entity index (`data/<user>/entity_index/<group>.json`) for cross-matter entity joins. New indexing builds it automatically via `do_index_group`; this is for existing groups. |

## Deploy / appliance
| File | Purpose |
|---|---|
| `provision.sh` | One command to turn a bare 12 GB box into a running appliance: installs deps, pre-caches the cross-encoder, renders the systemd unit. `--install-service` (privileged, opt-in) imports the offline model bundle, installs + enables the service, health-checks. Idempotent. |
| `bundle_ollama_models.sh` | **Build-box** step: pack the chat/embed/graph model blobs + manifests from the local Ollama store into `deploy/ollama-bundle/` so a deploy box needs **no `ollama pull` and no internet** ("model already packed"). Unprivileged (blobs are world-readable). Imported by `provision.sh --install-service`. |
| `load_test.py` | Concurrency load test: 1/2/3 users against the real `/ws/chat` pipeline. Reports TTFT, total latency, peak VRAM, errors/OOM. Run on the box with the GPU otherwise idle (one-slot rule). |
| `scale_test.py` | Retrieval-at-scale probe (GPU-free): times ChromaDB ANN search vs corpus size up to 20k vectors — the only corpus-size-dependent retrieval cost. Confirms search stays sub-10 ms at scale. |
| `sandbox.sh` | **Run the app on throwaway data** at `127.0.0.1:8380` (`SANDBOX_DIR`, default `/tmp/pdm-law-sandbox`; `reset` wipes it). Own users file, audit log, settings and JWT secret; signs in as `demo` / `meridiaan-demo-2025` (role user, so no legacy migration). Use it for UI work and screenshots, never the real `data/`. |
| `seed_demo.py` | **Seed the live general full-law demo** (`--variant en-fulllaw\|nl-fulllaw`, default en-fulllaw): two walled lawyer accounts + **three hero matters from different practice areas** (employment 2025.0108, workplace-injury/tort 2025.0116, corporate/shareholders 2025.0113) from `../../legal_corpus/variants/{en,nl}-fulllaw`. `j.havelaar` owns employment+injury, `m.terhorst` owns the corporate matter (EN accounts carry `_en`). Reset index also builds the entity index + stores `matter`. Pins the good models (gemma4:e4b + qwen3-embedding:4b). GPU-serial; run with the GPU idle. |
| `verify_demo.py` | Verify the general demo (`--variant`) over the real `/ws/chat` path: each hero query returns a grounded, cited answer on the planted fact (12-month non-compete / €310.000 tort under Art. 6:162 DCC / sole director van den Berg under Art. 2:9 DCC), plus the ethical-wall check (one lawyer cannot read the other's dossier). |
