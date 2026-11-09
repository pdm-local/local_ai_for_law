#!/usr/bin/env python3
"""Retrieval-at-scale probe — does vector search stay fast as the firm's archive grows?

The full index pipeline runs an LLM per document (card generation), so building a real 10k-doc
index is GPU-hours and irrelevant to the *latency* question. The only retrieval cost that grows
with corpus size is the ChromaDB ANN search over the chunk vectors; the reranker and the LLM
answer are fixed-cost (they see a fixed top-k). This probe isolates that growing cost: it fills a
throwaway Chroma collection (same config as the app — cosine HNSW, qwen3-embedding dim) with up to
N synthetic vectors and times the query at each size. GPU-free and fast.

  PYTHONPATH=<repo-parent> venv/bin/python scripts/scale_test.py
  SIZES="500 2000 5000 10000 20000" DIM=2560 scale_test.py
"""
from __future__ import annotations

import os
import statistics
import tempfile
import time

import numpy as np
import chromadb

DIM = int(os.getenv("DIM", "2560"))           # qwen3-embedding:4b dimension
SIZES = [int(x) for x in os.getenv("SIZES", "500 2000 5000 10000 20000").split()]
N_QUERIES = int(os.getenv("N_QUERIES", "20"))
TOP_K = int(os.getenv("TOP_K", "20"))
BATCH = 2000


def main() -> int:
    rng = np.random.default_rng(0)
    tmp = tempfile.mkdtemp(prefix="scale_chroma_")
    client = chromadb.PersistentClient(path=tmp)
    col = client.create_collection("scale", metadata={"hnsw:space": "cosine"})

    print(f"Retrieval-at-scale: dim={DIM} top_k={TOP_K} queries/size={N_QUERIES}")
    print(f"{'vectors':>8} {'add(s)':>7} {'search med':>11} {'search p95':>11}")
    added = 0
    for target in SIZES:
        t_add = time.time()
        while added < target:
            n = min(BATCH, target - added)
            embs = rng.standard_normal((n, DIM)).astype("float32")
            embs /= np.linalg.norm(embs, axis=1, keepdims=True)
            col.add(ids=[f"v{added+i}" for i in range(n)],
                    embeddings=embs.tolist(),
                    metadatas=[{"doc": f"d{(added+i)//8}"} for i in range(n)])
            added += n
        add_s = time.time() - t_add

        lat = []
        for _ in range(N_QUERIES):
            qv = rng.standard_normal(DIM).astype("float32"); qv /= np.linalg.norm(qv)
            t0 = time.time()
            col.query(query_embeddings=[qv.tolist()], n_results=TOP_K)
            lat.append((time.time() - t0) * 1000)
        med = statistics.median(lat)
        p95 = sorted(lat)[int(0.95 * len(lat)) - 1]
        print(f"{added:>8} {add_s:>7.1f} {med:>9.1f}ms {p95:>9.1f}ms")

    print("\nNote: search is the only corpus-size-dependent retrieval cost; rerank + LLM answer "
          "are fixed (top-k). Sub-second search at 10k+ vectors → archive size is not the bottleneck.")
    import shutil; shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
