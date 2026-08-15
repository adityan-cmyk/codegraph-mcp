"""Reinforcement agent — improves embedding search quality over time.

Runs a background loop that:
1. Evaluates pending AI feedback through quality gating
2. Aggregates accepted feedback into per-symbol boost/penalty weights
3. Auto-triggers a new build when 10+ accepted feedbacks accumulate
4. Monitors active build quality vs parent build quality
5. Triggers rollback if build quality regresses
6. Logs quality metrics

The agent does NOT retrain the embedding model. Instead it applies
a reranking layer on top of Weaviate results using learned weights,
which means every search gets smarter as more feedback accumulates.
"""

import logging
import threading
import time

from app.rag.reinforcement import ai_feedback_store, build_registry, feedback_store

logger = logging.getLogger(__name__)

_AGENT_INTERVAL = 300  # 5 minutes
_ROLLBACK_THRESHOLD = 0.15  # if new build score is 15%+ worse than parent, rollback
_RERANK_THRESHOLD = 5  # trigger reranking refresh at 5 accepted feedbacks
_AUTOBUILD_THRESHOLD = 10  # trigger full rebuild at 10 accepted feedbacks
_REBUILD_LOCK = threading.Lock()
_RERANK_LOCK = threading.Lock()
_RUNNING = False
_THREAD: threading.Thread | None = None
_last_rerank_count = 0


def _trigger_rebuild(feedback_entries: list[dict]) -> None:
    """Trigger a zero-downtime rebuild, consuming the given feedback."""
    if not _REBUILD_LOCK.acquire(blocking=False):
        logger.info("Rebuild already in progress, skipping auto-trigger")
        return
    try:
        feedback_ids = [f["feedback_id"] for f in feedback_entries]
        logger.info("Auto-triggering rebuild with %d feedback entries: %s", len(feedback_ids), feedback_ids)

        from app.rag.indexing_service import reindex_semantic_only
        reindex_semantic_only()

        from app.rag.reinforcement import build_registry
        active = build_registry.get_active_build()
        if active:
            ai_feedback_store.mark_feedback_consumed(feedback_ids, active["build_id"])
            logger.info("Marked %d feedback entries as consumed by build %s", len(feedback_ids), active["build_id"])
    except Exception:
        logger.exception("Auto-triggered rebuild failed")
    finally:
        _REBUILD_LOCK.release()


def _agent_tick():
    """Single iteration of the reinforcement loop."""
    # 1. Evaluate pending AI feedback through quality gate
    try:
        result = ai_feedback_store.evaluate_pending_feedback()
        if result["evaluated"] > 0:
            logger.info(
                "Reinforcement: evaluated %d feedback entries (%d accepted, %d rejected)",
                result["evaluated"], result["accepted"], result["rejected"],
            )
    except Exception:
        logger.debug("Feedback evaluation failed", exc_info=True)

    # 2. Sync accepted AI feedback into per-symbol boost weights
    try:
        accepted = ai_feedback_store.get_accepted_feedback(limit=50)
        if accepted:
            signals = ai_feedback_store.extract_symbol_signals_from_feedback(accepted)
            if signals:
                for symbol_id, weight in signals.items():
                    feedback_store.record_feedback(
                        query_text="_ai_feedback",
                        symbol_id=symbol_id,
                        original_score=0.5,
                        feedback=1 if weight > 0 else -1,
                        reason=f"AI feedback signal (weight={weight:.3f})",
                    )
                logger.info("Reinforcement: applied %d symbol signals from AI feedback", len(signals))
                feedback_ids = [f["feedback_id"] for f in accepted]
                active = build_registry.get_active_build()
                if active:
                    ai_feedback_store.mark_feedback_consumed(feedback_ids, active["build_id"])
                    logger.info("Marked %d AI feedback entries as consumed (signals applied)", len(feedback_ids))
    except Exception:
        logger.debug("Signal extraction failed", exc_info=True)

    # 3. Incremental feedback consumption (Idea 5)
    try:
        stats = ai_feedback_store.get_feedback_stats()
        unconsumed = stats.get("unconsumed_accepted", 0)

        # At 10: full rebuild
        if unconsumed >= _AUTOBUILD_THRESHOLD:
            logger.info("Reinforcement: %d unconsumed accepted feedbacks >= threshold %d — triggering full rebuild", unconsumed, _AUTOBUILD_THRESHOLD)
            accepted_entries = ai_feedback_store.get_accepted_feedback(limit=_AUTOBUILD_THRESHOLD)
            _trigger_rebuild(accepted_entries)
            global _last_rerank_count
            _last_rerank_count = 0

        # At 5 (and not yet at 10): reranking refresh (no re-embedding, just re-sort)
        elif unconsumed >= _RERANK_THRESHOLD and unconsumed > _last_rerank_count:
            if _RERANK_LOCK.acquire(blocking=False):
                try:
                    logger.info("Reinforcement: %d unconsumed accepted feedbacks >= rerank threshold %d — refreshing reranking weights", unconsumed, _RERANK_THRESHOLD)
                    accepted = ai_feedback_store.get_accepted_feedback(limit=unconsumed)
                    signals = ai_feedback_store.extract_symbol_signals_from_feedback(accepted)
                    if signals:
                        for symbol_id, weight in signals.items():
                            feedback_store.record_feedback(
                                query_text="_rerank_refresh",
                                symbol_id=symbol_id,
                                original_score=0.5,
                                feedback=1 if weight > 0 else -1,
                                reason=f"Reranking refresh signal (weight={weight:.3f})",
                            )
                    _last_rerank_count = unconsumed
                    logger.info("Reinforcement: reranking weights refreshed with %d signals", len(signals))
                finally:
                    _RERANK_LOCK.release()
    except Exception:
        logger.debug("Auto-build check failed", exc_info=True)

    # 4. Check if active build quality has regressed
    try:
        active = build_registry.get_active_build()
        if active and active.get("quality_score") is not None and active.get("parent_quality_score") is not None:
            current_score = float(active["quality_score"])
            parent_score = float(active["parent_quality_score"])
            if parent_score > 0 and current_score < parent_score - _ROLLBACK_THRESHOLD:
                logger.warning(
                    "Build quality regression detected: current=%.3f parent=%.3f — triggering rollback",
                    current_score, parent_score,
                )
                from app.rag.indexing_service import rollback_last_build
                rollback_last_build(reason=f"quality score {current_score:.3f} < parent {parent_score:.3f}")
    except Exception:
        logger.debug("Build quality check failed", exc_info=True)

    # 5. Log stats
    try:
        fb_stats = ai_feedback_store.get_feedback_stats()
        build_stats = build_registry.get_build_stats()
        if fb_stats["total_feedback"] > 0 or build_stats["total_builds"] > 0:
            logger.info(
                "Reinforcement stats: feedback(pending=%d accepted=%d rejected=%d consumed=%d) builds(active=%d rolled_back=%d) autobuild_threshold=%d",
                fb_stats["pending"], fb_stats["accepted"],
                fb_stats["rejected"], fb_stats["consumed"],
                build_stats["active"], build_stats["rolled_back"],
                _AUTOBUILD_THRESHOLD,
            )
    except Exception:
        logger.debug("Stats logging failed", exc_info=True)


def _agent_loop():
    while _RUNNING:
        _agent_tick()
        for _ in range(_AGENT_INTERVAL):
            if not _RUNNING:
                break
            time.sleep(1)


def start_agent():
    """Start the reinforcement agent in a daemon thread."""
    global _RUNNING, _THREAD
    if _RUNNING:
        return
    _RUNNING = True
    _THREAD = threading.Thread(target=_agent_loop, daemon=True, name="reinforcement-agent")
    _THREAD.start()
    logger.info("Reinforcement agent started (interval=%ds)", _AGENT_INTERVAL)


def stop_agent():
    global _RUNNING
    _RUNNING = False
    if _THREAD:
        _THREAD.join(timeout=10)
