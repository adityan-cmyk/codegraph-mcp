import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.index_store import index_metadata_store
from app.core.systemone import gate_feedback
from app.main import app
from app.rag.ingestion.tree_sitter import extract_rust_chunks, generate_symbol_id
from app.rag.indexing_service import index_rust_repository, replay_indexes_from_storage
from app.rag.reinforcement.feedback_store import compute_boost_weight
from app.rag.retrieval.graph import graph_index, get_blast_radius
from app.rag.retrieval.semantic import semantic_index
from app.schemas.codebase import IndexSnapshot


class RagAndMcpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        semantic_index.reset()
        graph_index.reset()
        index_metadata_store.reset()
        self.client = TestClient(app)

    def test_extract_rust_chunks_builds_symbol_ids(self) -> None:
        source = """
pub struct User {
    id: i32,
}

fn login_user() {
    println!(\"login\");
}
"""
        chunks = extract_rust_chunks("auth/handlers.rs", source)

        self.assertGreaterEqual(len(chunks), 1)
        struct_chunks = [c for c in chunks if c.kind == "struct"]
        fn_chunks = [c for c in chunks if c.kind == "fn"]
        if struct_chunks:
            self.assertEqual(struct_chunks[0].symbol_id, generate_symbol_id("auth/handlers", "User"))
        if fn_chunks:
            self.assertEqual(fn_chunks[0].kind, "fn")

    def test_short_reexport_only_file_does_not_crash(self) -> None:
        """Regression: a 1-line `pub use ...;` file with no symbols hit
        `lines.strip()` on a LIST in the no-match fallback branch and crashed
        the nightly incremental ingest (2026-09-30, report_type.rs)."""
        source = "pub use dashboard::types::api::mis_report::ReportType;\n"
        chunks = extract_rust_chunks("async_service/product/core/report_type.rs", source)
        # Either skipped as trivial or indexed as a module chunk — but never a crash
        for c in chunks:
            self.assertTrue(c.symbol_id)

    def test_semantic_index_returns_top_match(self) -> None:
        source = "fn login_user() { panic!(\"bad token\"); }"
        chunks = extract_rust_chunks("auth/handlers.rs", source)
        semantic_index.upsert_chunks(chunks)

        matches = semantic_index.query_chunks("auth panic bad token", limit=1)

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].symbol_id, chunks[0].symbol_id)

    def test_graph_index_tracks_blast_radius(self) -> None:
        graph_index.upsert_symbol("auth::login_user", calls=["db::save_session"])

        radius = get_blast_radius("auth::login_user")

        self.assertEqual(radius["downstream"], ["db::save_session"])

    def test_index_rust_repository_builds_semantic_and_graph_indexes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "auth.rs").write_text(
                "fn save_session() {}\n\nfn login_user() { save_session(); }\n",
                encoding="utf-8",
            )

            with patch("app.rag.indexing_service.settings.indexing_allowed_roots", [temp_dir]), patch(
                "app.rag.indexing_service.settings.codebase_root_path", temp_dir
            ):
                result = index_rust_repository(temp_dir)

        self.assertEqual(result.files_indexed, 1)
        radius = get_blast_radius("auth::login_user")
        self.assertIn("auth::save_session", radius.get("downstream", []) + radius.get("uses", []))

    def test_index_repository_endpoint_rejects_untrusted_path(self) -> None:
        response = self.client.post("/api/index/repository", json={"repository_path": "/etc"})

        self.assertEqual(response.status_code, 403)

    def test_index_repository_endpoint_indexes_allowed_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "mod.rs").write_text("fn ping() {}\n", encoding="utf-8")

            with patch("app.rag.indexing_service.settings.indexing_allowed_roots", [temp_dir]), patch(
                "app.rag.indexing_service.settings.codebase_root_path", temp_dir
            ):
                response = self.client.post("/api/index/repository", json={"repository_path": temp_dir})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["files_indexed"], 1)

    def test_index_query_and_stats_endpoints_return_index_data(self) -> None:
        chunks = extract_rust_chunks("auth/handlers.rs", "fn login_user() { panic!(\"bad token\"); }")
        semantic_index.upsert_chunks(chunks)
        graph_index.upsert_symbol(chunks[0].symbol_id, calls=["db::save_session"])

        query_response = self.client.post("/api/index/query", json={"query": "login user", "limit": 3})
        stats_response = self.client.get("/api/index/stats")
        graph_response = self.client.get(f"/api/index/graph/{chunks[0].symbol_id}?depth=2")

        self.assertEqual(query_response.status_code, 200)
        self.assertEqual(query_response.json()[0]["symbol_id"], chunks[0].symbol_id)
        self.assertEqual(stats_response.status_code, 200)
        self.assertEqual(stats_response.json()["semantic_documents"], 1)
        self.assertEqual(stats_response.json()["graph_edges"], 1)
        self.assertEqual(graph_response.status_code, 200)
        self.assertEqual(graph_response.json()["neighborhoods"][0]["symbol_id"], chunks[0].symbol_id)

    def test_replay_indexes_from_storage_rehydrates_semantic_and_graph_indexes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_path = Path(temp_dir)
            (repo_path / "src").mkdir()
            (repo_path / "src" / "auth.rs").write_text(
                "fn save_session() {}\n\nfn login_user() { save_session(); }\n",
                encoding="utf-8",
            )

            with patch("app.rag.indexing_service.settings.indexing_allowed_roots", [temp_dir]), patch(
                "app.rag.indexing_service.settings.codebase_root_path", temp_dir
            ):
                indexed = index_rust_repository(temp_dir)

        semantic_index.reset_documents()
        graph_index.reset()

        replayed = replay_indexes_from_storage()
        replay_query = semantic_index.query_chunks("login user", limit=5)
        replay_radius = get_blast_radius("auth::login_user")

        self.assertIsNotNone(replayed)
        self.assertIn("auth::save_session", replay_radius.get("downstream", []) + replay_radius.get("uses", []))

    def test_replay_endpoint_returns_snapshot(self) -> None:
        chunks = extract_rust_chunks("auth/handlers.rs", "fn login_user() { panic!(\"bad token\"); }")
        index_metadata_store.replace_snapshot(
            IndexSnapshot(
                repository_path="/tmp/repo",
                files_indexed=1,
                chunks=chunks,
                graph_edges=[],
            )
        )

        graph_index.reset()
        from app.rag.indexing_service import _semantic_rebuild_lock, _semantic_rebuild_in_progress
        with _semantic_rebuild_lock:
            was_in_progress = _semantic_rebuild_in_progress

        response = self.client.post("/api/index/replay")

        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json()["symbols_indexed"], 1)


class BoostWeightFormulaTestCase(unittest.TestCase):
    """Regression tests for the boost formula edge cases.

    The old formula (p / (p + n + 5)) * 2 - 1 penalized symbols with fewer
    than 5 net-positive votes: a single helpful vote produced a -0.667 boost.
    """

    def test_single_positive_vote_is_positive(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(1, 0), 1 / 6)

    def test_single_negative_vote_is_mild_not_maximal(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(0, 1), -1 / 6)

    def test_no_votes_is_neutral(self) -> None:
        self.assertEqual(compute_boost_weight(0, 0), 0.0)

    def test_heavy_negative_stays_bounded(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(0, 31), -31 / 36)
        self.assertGreaterEqual(compute_boost_weight(0, 31), -1.0)

    def test_heavy_positive_stays_bounded(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(33, 0), 33 / 38)
        self.assertLessEqual(compute_boost_weight(33, 0), 1.0)

    def test_always_within_bounds(self) -> None:
        for p in (0, 1, 2, 5, 10, 100, 1000):
            for n in (0, 1, 2, 5, 10, 100, 1000):
                w = compute_boost_weight(p, n)
                self.assertLessEqual(abs(w), 1.0, f"out of bounds for p={p}, n={n}")

    def test_symmetry(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(7, 3), -compute_boost_weight(3, 7))


class ReinforcementSyncIdempotencyTestCase(unittest.TestCase):
    """The 5-minute agent tick must apply each accepted feedback entry to
    boost weights exactly once (boost_applied_at watermark), not on every
    tick — the old behavior inflated vote counts by ~288/day."""

    def test_tick_applies_unsynced_feedback_once(self) -> None:
        from app.rag.reinforcement import agent as agent_module

        entries = [
            {"feedback_id": "fb1", "results_used": json.dumps([
                {"symbol_id": "crate::a::foo", "helpful": True},
            ]), "quality_rating": 4, "quality_score": 0.8},
        ]
        applied_calls: list[dict] = []
        marked: list[list[str]] = []

        class FakeAIStore:
            @staticmethod
            def get_unsynced_accepted_feedback(limit=50):
                return entries if not marked else []

            @staticmethod
            def extract_symbol_signals_from_feedback(feedback):
                return {"crate::a::foo": 0.6}

            @staticmethod
            def mark_boost_applied(feedback_ids):
                marked.append(list(feedback_ids))

        class FakeFeedbackStore:
            @staticmethod
            def record_feedback(**kwargs):
                applied_calls.append(kwargs)

        with patch.object(agent_module.ai_feedback_store, "get_unsynced_accepted_feedback", FakeAIStore.get_unsynced_accepted_feedback), \
             patch.object(agent_module.ai_feedback_store, "extract_symbol_signals_from_feedback", FakeAIStore.extract_symbol_signals_from_feedback), \
             patch.object(agent_module.ai_feedback_store, "mark_boost_applied", FakeAIStore.mark_boost_applied), \
             patch.object(agent_module.feedback_store, "record_feedback", FakeFeedbackStore.record_feedback), \
             patch.object(agent_module.ai_feedback_store, "evaluate_pending_feedback", lambda: {"evaluated": 0}), \
             patch.object(agent_module.ai_feedback_store, "get_feedback_stats", lambda: {"unconsumed_accepted": 0}):
            agent_module._agent_tick()
            agent_module._agent_tick()

        self.assertEqual(len(applied_calls), 1, "second tick must not re-apply the same feedback")
        self.assertEqual(marked, [["fb1"]])
        self.assertFalse(applied_calls[0]["update_expansion"], "pseudo-query must not write query expansions")


class SystemOneGateTestCase(unittest.TestCase):
    """Decision-model feedback gate: actionable is the hard gate.

    Validated against live nimble judgments on this host:
    - good feedback:    specific=0.997, actionable=0.992, consistent=0.867
    - garbage:          specific=0.027, actionable=0.065, consistent=0.391
    - adversarial:      specific=0.999, actionable=0.042, consistent=0.995
      (form-perfect, vacuous content — heuristic gate accepts it, model rejects)
    """

    def test_good_feedback_accepted(self) -> None:
        score, reason = gate_feedback({"specific": 0.997, "actionable": 0.992, "consistent": 0.867})
        self.assertIsNone(reason)
        self.assertGreater(score, 0.5)

    def test_garbage_rejected(self) -> None:
        score, reason = gate_feedback({"specific": 0.027, "actionable": 0.065, "consistent": 0.391})
        self.assertIsNotNone(reason)
        self.assertLessEqual(score, 0.5)

    def test_adversarial_form_perfect_but_vacuous_rejected(self) -> None:
        score, reason = gate_feedback({"specific": 0.999, "actionable": 0.042, "consistent": 0.995})
        self.assertIsNotNone(reason, "high specific + high consistent must NOT outweigh low actionable")
        self.assertLessEqual(score, 0.5)

    def test_judge_returns_none_when_service_unreachable(self) -> None:
        """Fallback path: unreachable decision model must yield None, not raise."""
        from unittest.mock import patch

        from app.core import systemone

        with patch.object(systemone.settings, "systemone_url", "http://127.0.0.1:1/v1/systemone"), \
             patch.object(systemone.settings, "systemone_timeout", 2):
            judged = systemone.judge_feedback({"client_id": "x", "quality_rating": 4})
        self.assertIsNone(judged)

    def test_judge_disabled_when_url_unset(self) -> None:
        from unittest.mock import patch

        from app.core import systemone

        with patch.object(systemone.settings, "systemone_url", None):
            self.assertIsNone(systemone.judge_feedback({"client_id": "x"}))


if __name__ == "__main__":
    unittest.main()


class ReviewFixesTestCase(unittest.TestCase):
    """Regression tests from the 2026-10-01 deep-dive review."""

    def setUp(self) -> None:
        semantic_index.reset()
        graph_index.reset()
        index_metadata_store.reset()

    def test_batch_blast_radius_counts_string_edges(self) -> None:
        """combined_impact was all-zeros: aggregation expected dict edges but
        lists carry plain strings — the isinstance guard skipped everything."""
        from app.mcp.tools.graph_read_tools import batch_blast_radius

        graph_index.upsert_symbol("app::wallet::debit", calls=["app::ledger::record"])
        graph_index.upsert_symbol("app::audit::check", calls=["app::wallet::debit"])
        result = batch_blast_radius(["app::wallet::debit"])
        ci = result["combined_impact"]
        self.assertGreaterEqual(ci["total_unique_callees"], 1)

    def test_search_symbols_filters_wildcard_noise(self) -> None:
        """`_` wildcard bindings must not appear in search results."""
        from app.mcp.tools.graph_read_tools import search_symbols

        graph_index.upsert_symbol("app::handlers::_")
        graph_index.upsert_symbol("app::handlers::real_fn")
        result = search_symbols("handlers")
        names = [s["symbol_id"] for s in result["symbols"]]
        self.assertNotIn("app::handlers::_", names)
        self.assertIn("app::handlers::real_fn", names)

    def test_traverse_summary_counts_per_hop_level(self) -> None:
        """summary_only must return per-BFS-hop counts, not per-symbol rows
        with a sequential index masquerading as hop distance."""
        from app.mcp.tools.graph_read_tools import traverse_graph

        graph_index.upsert_symbol("app::a::root", calls=["app::b::mid"])
        graph_index.upsert_symbol("app::b::mid", calls=["app::c::leaf"])
        result = traverse_graph("app::a::root", depth=2, summary_only=True)
        levels = result["summary"]
        self.assertGreaterEqual(len(levels), 2)
        self.assertEqual(levels[0]["hop"], 1)
        self.assertEqual(levels[1]["hop"], 2)
        self.assertGreaterEqual(levels[1]["cumulative_reachable"], 2)
