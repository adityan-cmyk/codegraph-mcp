#!/usr/bin/env python3
"""Embedding model trial — candidate vs live model on the golden set.

Downloads the candidate model inside the reranker container (which already
has sentence-transformers), embeds the golden queries and their target
chunks, and reports hit@5 / hit@10 / MRR next to the live model's golden
eval score (GET /api/index/eval/golden) for a side-by-side comparison.

Usage: python3 scripts/embedding-trial.py <candidate-model>
Default candidate: jinaai/jina-embeddings-v2-base-code (code-tuned).
"""

import json
import os
import sys
import urllib.request

CANDIDATE = sys.argv[1] if len(sys.argv) > 1 else "jinaai/jina-embeddings-v2-base-code"
HOST = os.environ.get("API_URL", "http://localhost:8000")
TOKEN = ""
for line in open(os.path.join(os.path.dirname(__file__), "..", ".env")):
    if line.startswith("API_AUTH_TOKEN="):
        TOKEN = line.split("=", 1)[1].strip()

TRIAL_SCRIPT = r'''
import json, sys, urllib.request

candidate = sys.argv[1]
req = urllib.request.Request("http://host.docker.internal:8000/api/index/eval/golden/stats")
golden_count = 0

from sentence_transformers import SentenceTransformer
model = SentenceTransformer(candidate)

pairs = json.load(open("/tmp/golden.json"))
queries = sorted({p["query"] for p in pairs})
symbols = sorted({p["symbol_id"] for p in pairs})

q_vecs = model.encode(queries, normalize_embeddings=True, show_progress_bar=False)
s_vecs = model.encode(symbols, normalize_embeddings=True, show_progress_bar=False)

# for each pair: rank of the expected symbol among all golden symbols
import numpy as np
q_index = {q: i for i, q in enumerate(queries)}
s_index = {s: i for i, s in enumerate(symbols)}
hits5 = hits10 = 0
mrr = 0.0
for p in pairs:
    sims = s_vecs @ q_vecs[q_index[p["query"]]]
    ranked = sorted(range(len(symbols)), key=lambda i: -sims[i])
    rank = ranked.index(s_index[p["symbol_id"]]) + 1
    if rank <= 5: hits5 += 1
    if rank <= 10: hits10 += 1
    mrr += 1.0 / rank
n = len(pairs)
print(json.dumps({
    "candidate": candidate,
    "golden_pairs": n,
    "note": "retrieval over golden SYMBOL IDS + queries only — a quick proxy, not the full corpus",
    "hit_at_5": round(hits5 / n, 4),
    "hit_at_10": round(hits10 / n, 4),
    "mrr": round(mrr / n, 4),
}))
'''


def main() -> int:
    # fetch golden pairs from the live eval
    req = urllib.request.Request(f"{HOST}/api/index/eval/golden/stats",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
    stats = json.load(urllib.request.urlopen(req, timeout=15))
    print("golden pairs available:", stats.get("golden_pairs"))

    # full pairs via the admin usage path? golden_eval only exposes stats;
    # pull pairs through the eval runner's builder instead
    fetch = r'''
import json, sys
sys.path.insert(0, "/app")
from app.rag.reinforcement.golden_eval import build_golden_set
print(json.dumps(build_golden_set()))
'''
    out = os.popen(
        f"docker exec oncall-backend python -c {shlex_quote(fetch)}"
    ).read()
    try:
        pairs = json.loads(out.strip().splitlines()[-1])
    except Exception:
        print("could not fetch golden pairs from backend:", out[:300])
        return 1
    print(f"pulled {len(pairs)} golden pairs")

    with open("/tmp/opencode/golden.json", "w") as f:
        json.dump(pairs, f)

    with open("/tmp/opencode/trial_inner.py", "w") as f:
        f.write(TRIAL_SCRIPT)

    os.system(
        f"docker cp /tmp/opencode/golden.json oncall-reranker:/tmp/golden.json && "
        f"docker cp /tmp/opencode/trial_inner.py oncall-reranker:/tmp/trial_inner.py && "
        f"docker exec oncall-reranker python /tmp/trial_inner.py {CANDIDATE}"
    )
    return 0


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    sys.exit(main())
