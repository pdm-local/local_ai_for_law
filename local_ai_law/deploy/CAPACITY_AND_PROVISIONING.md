# Capacity & provisioning validation

Records the go-live capacity probes (#81) and the clean-box provisioning dry-run (#25),
run on the 12 GB-VRAM box (RTX 4080 laptop), 2026-07-07. Reproduce with the scripts named below.

## Concurrency — `scripts/load_test.py` (1/2/3 lawyers at once)

Real serving path (`/ws/chat` → retrieval → Ollama generation), `GROUP=caa865b5`
(nl-realistic, 459 docs), `chat_model=gemma4:e4b`, `top_k=8`.

| concurrency | ok | err | TTFT median | total median | total max | peak VRAM |
|---|---|---|---|---|---|---|
| 1 | 1 | 0 |  9.8s | 15.2s | 15.2s | 6191 MiB |
| 2 | 2 | 0 | 13.2s | 25.1s | 34.3s | 6193 MiB |
| 3 | 3 | 0 | 23.7s | 31.6s | 42.2s | 6193 MiB |

**Result: PASS — no errors/OOM at any level.** VRAM is flat (~6.2 GB): the app's GPU gate
serialises generation (one slot), so added users **queue** rather than contend. That queueing is
the only cost — the 3rd concurrent user waits ~24 s for first token. **Honest capacity read: ~2–3
concurrent lawyers per box** before first-token latency gets uncomfortable; beyond that, add a box
(or a second GPU) rather than expecting parallel generation.

## Retrieval at scale — `scripts/scale_test.py` (GPU-free)

Isolates the only corpus-size-dependent retrieval cost: ChromaDB ANN search over chunk vectors
(rerank + LLM answer are fixed-cost at fixed top-k). Synthetic vectors, dim 2560, top_k 20.

| vectors | add | search median | search p95 |
|---|---|---|---|
| 500 | 0.2s | 0.9ms | 1.4ms |
| 2 000 | 0.5s | 1.5ms | 2.4ms |
| 5 000 | 1.0s | 1.9ms | 2.2ms |
| 10 000 | 2.0s | 2.3ms | 2.6ms |
| 20 000 | 5.0s | 2.8ms | 3.5ms |

**Archive size is not the bottleneck** — search stays low-single-digit ms at 10k–20k vectors.
(A real 10k-doc *index* is GPU-hours of card generation and irrelevant to the latency question,
which is why this probe is synthetic — see the script's docstring.)

## Clean-box provisioning dry-run — `scripts/provision.sh` (#25)

Non-mutating validation on this box (no re-pip, no model pull, no systemd changes):

- **[1] script parses** (`bash -n`).
- **[2] runtime models present**: `gemma4:e4b`, `qwen3-embedding:4b`, `gemma4:e2b` — all OK.
- **[3] cross-encoder OFF (optional, by design)**: `sentence_transformers`/`torch` are not in
  `requirements.txt`; `cross_encoder.available()` gates the reranker, and retrieval fails open to
  the Ollama reranker + hybrid ordering. **Fixed a stale provisioning step here**: `provision.sh`
  step 3 used to unconditionally pre-cache the cross-encoder and WARN on every clean box; it now
  detects the optional deps and prints an honest "OFF unless you install them" note instead.
- **[4] systemd unit renders** from `deploy/pdm-legal.service.template` with `ExecStart` present,
  no unresolved placeholders.
- **[5] health endpoint green**: `/api/health` → 200 (the check `--install-service` waits on).
- **[6] install path present**: `run.sh install` (idempotent) is the single source of truth for deps.

**Result: a clean box provisioned with `provision.sh` comes up.** The cross-encoder is an opt-in
enhancement (`pip install sentence_transformers torch`, then re-run) — the base appliance runs
without it, which is also the configuration the benchmarks were measured under.
