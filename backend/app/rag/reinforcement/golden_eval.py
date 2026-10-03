"""Golden eval set — search quality measured against real agent feedback.

Every +1 search_feedback row is a human/agent-verified (query, symbol) pair:
the agent searched for that meaning and confirmed that symbol was the right
answer. Those pairs ARE the golden set — no hand-labeling required.

run_eval() replays each query through the full search pipeline (boosts,
expansions, graph fallback — exactly what agents hit) and scores hit@5,
hit@10 and MRR. The score feeds build_registry.quality_score so quality is
tracked per build and drift between builds is visible.
"""

import logging

logger = logging.getLogger(__name__)

_INTERNAL_QUERY_PREFIXES = ("_", "test ")


def build_golden_set() -> list[dict[str, str]]:
    """(query, symbol) pairs with +1 feedback, filtered and deduped."""
    from app.rag.reinforcement import feedback_store
    from app.rag.retrieval.graph import graph_index

    pairs: dict[tuple[str, str], dict[str, str]] = {}
    with feedback_store._connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT query_text, symbol_id FROM search_feedback WHERE feedback = 1 AND symbol_id IS NOT NULL"
            )
            rows = cur.fetchall()
    for row in rows:
        query = (row["query_text"] or "").strip()
        symbol = (row["symbol_id"] or "").strip()
        if not query or any(query.lower().startswith(p) for p in _INTERNAL_QUERY_PREFIXES):
            continue
        if feedback_store.is_pseudo_symbol(symbol):
            continue
        if not graph_index.has_symbol(symbol):
            continue  # renamed or deleted since — not a fair label
        pairs.setdefault((query, symbol), {"query": query, "symbol_id": symbol})
    return list(pairs.values())


def run_eval(top_k: int = 10, limit: int = 200) -> dict[str, object]:
    """Replay golden queries through the live search pipeline and score."""
    from app.mcp.tools.graph_read_tools import semantic_search

    golden = build_golden_set()[:limit]
    if not golden:
        return {"error": "golden set is empty — no verified (query, symbol) pairs yet"}

    hits5 = 0
    hits10 = 0
    mrr_sum = 0.0
    misses: list[dict[str, str]] = []
    for item in golden:
        try:
            result = semantic_search(item["query"], limit=top_k)
        except Exception as exc:
            logger.warning("golden eval: search failed for %r: %s", item["query"], exc)
            continue
        matches = result.get("results") or []
        rank = 0
        for idx, match in enumerate(matches[:top_k], start=1):
            if match.get("symbol_id") == item["symbol_id"]:
                rank = idx
                break
        if rank:
            mrr_sum += 1.0 / rank
            if rank <= 5:
                hits5 += 1
            hits10 += 1
        else:
            misses.append({"query": item["query"], "expected": item["symbol_id"]})

    n = len(golden)
    score = {
        "golden_pairs": n,
        "hit_at_5": round(hits5 / n, 4),
        "hit_at_10": round(hits10 / n, 4),
        "mrr": round(mrr_sum / n, 4),
        "misses": misses[:20],
        "miss_count": len(misses),
    }

    try:
        from app.rag.reinforcement import build_registry
        active = build_registry.get_active_build()
        if active:
            quality = score["hit_at_10"]
            build_registry.update_quality_score(active["build_id"], quality)
            parent_quality = active.get("parent_quality_score")
            score["build_id"] = active["build_id"]
            score["recorded_quality"] = quality
            if parent_quality is not None:
                delta = quality - float(parent_quality)
                score["parent_quality"] = float(parent_quality)
                score["quality_delta"] = round(delta, 4)
                if delta < -0.10:
                    logger.warning(
                        "Golden eval QUALITY DROP: hit@10 %.3f vs parent %.3f (%.1f%%)",
                        quality, float(parent_quality), delta * 100,
                    )
    except Exception:
        logger.warning("golden eval: failed to record quality score", exc_info=True)

    logger.info("Golden eval: %d pairs, hit@5=%.3f hit@10=%.3f mrr=%.3f",
                n, score["hit_at_5"], score["hit_at_10"], score["mrr"])
    return score


def get_golden_stats() -> dict[str, object]:
    try:
        return {"golden_pairs": len(build_golden_set())}
    except Exception as exc:
        return {"error": str(exc)}
