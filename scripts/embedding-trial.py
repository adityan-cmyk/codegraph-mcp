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
import json, sys

candidate = sys.argv[1]

from sentence_transformers import SentenceTransformer
model = SentenceTransformer(candidate, trust_remote_code=True)

# baseline: the ACTUAL live model (weaviate_semantic._EMBED_MODEL_NAME) under
# the SAME proxy — side by side. (An earlier run mislabeled MiniLM as the live
# model; the live model is bge-base-en-v1.5. Never hardcode this again.)
baseline = SentenceTransformer("BAAI/bge-base-en-v1.5")

pairs = json.load(open("/tmp/golden.json"))
queries = sorted({p["query"] for p in pairs})
symbols = sorted({p["symbol_id"] for p in pairs})

q_vecs = model.encode(queries, normalize_embeddings=True, show_progress_bar=False)
s_vecs = model.encode(symbols, normalize_embeddings=True, show_progress_bar=False)
bq_vecs = baseline.encode(queries, normalize_embeddings=True, show_progress_bar=False)
bs_vecs = baseline.encode(symbols, normalize_embeddings=True, show_progress_bar=False)

# for each pair: rank of the expected symbol among all golden symbols
import numpy as np
q_index = {q: i for i, q in enumerate(queries)}
s_index = {s: i for i, s in enumerate(symbols)}
def metrics(q_vecs_, s_vecs_):
    import numpy as np
    hits5 = hits10 = 0
    mrr = 0.0
    for p in pairs:
        sims = s_vecs_ @ q_vecs_[q_index[p["query"]]]
        ranked = sorted(range(len(symbols)), key=lambda i: -sims[i])
        rank = ranked.index(s_index[p["symbol_id"]]) + 1
        if rank <= 5: hits5 += 1
        if rank <= 10: hits10 += 1
        mrr += 1.0 / rank
    n = len(pairs)
    return {"hit_at_5": round(hits5 / n, 4), "hit_at_10": round(hits10 / n, 4), "mrr": round(mrr / n, 4)}

cand_m = metrics(q_vecs, s_vecs)
base_m = metrics(bq_vecs, bs_vecs)
print(json.dumps({
    "candidate": candidate,
    "baseline": "BAAI/bge-base-en-v1.5 (actual live model)",
    "golden_pairs": len(pairs),
    "note": "symbol-id-only retrieval proxy, both models under identical conditions",
    "candidate_metrics": cand_m,
    "baseline_metrics": base_m,
    "verdict": "candidate wins" if cand_m["mrr"] > base_m["mrr"] else "baseline wins",
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

    # runs in the backend container: bge-base is pre-cached there (HF_HOME),
    # candidates download on first use
    os.system(
        f"docker cp /tmp/opencode/golden.json oncall-backend:/tmp/golden.json && "
        f"docker cp /tmp/opencode/trial_inner.py oncall-backend:/tmp/trial_inner.py && "
        f"docker exec -e HF_HOME=/app/.cache/huggingface oncall-backend "
        f"python /tmp/trial_inner.py {CANDIDATE}"
    )
    return 0


def shlex_quote(s: str) -> str:
    return "'" + s.replace("'", "'\"'\"'") + "'"


if __name__ == "__main__":
    sys.exit(main())
