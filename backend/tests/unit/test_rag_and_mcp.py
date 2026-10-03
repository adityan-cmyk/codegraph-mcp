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
        self.assertAlmostEqual(compute_boost_weight(0, 1), -2 / 7)  # negatives weigh 2x (k=2)

    def test_no_votes_is_neutral(self) -> None:
        self.assertEqual(compute_boost_weight(0, 0), 0.0)

    def test_heavy_negative_stays_bounded(self) -> None:
        self.assertAlmostEqual(compute_boost_weight(0, 31), -62 / 67)  # k=2: neg=62
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
        # k=2 asymmetry is intentional (rich-get-richer correction):
        # corrections at half the positive rate neutralize exactly.
        self.assertAlmostEqual(compute_boost_weight(10, 5), 0.0)
        self.assertAlmostEqual(compute_boost_weight(3, 7), -0.5)


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


class FeedbackClassificationTestCase(unittest.TestCase):
    """Feedback routing: bug reports must not adjust symbol weights."""

    def test_classify_feedback_routes_tool_bug(self) -> None:
        from unittest.mock import patch

        from app.core import systemone

        bug_row = {
            "client_id": "agent", "pr_context": "test",
            "tools_called": [{"tool": "find_dependency_path"}],
            "results_used": [], "results_expected": "path routed through uuid_v7::now hub instead of the direct edge",
            "quality_rating": 3,
            "improvement_suggestions": "find_dependency_path returns noisy paths through summary nodes",
        }
        with patch.object(systemone.settings, "systemone_url", "http://x"), \
             patch.object(systemone, "decide", lambda state, questions: {"kind": {"type": "choice", "choice": "tool_bug"}}):
            self.assertEqual(systemone.classify_feedback(bug_row), "tool_bug")

    def test_classify_feedback_defaults_to_ranking_on_failure(self) -> None:
        from unittest.mock import patch

        from app.core import systemone

        with patch.object(systemone.settings, "systemone_url", None):
            self.assertEqual(systemone.classify_feedback({"client_id": "x"}), "ranking")

    def test_sync_reroutes_non_ranking_away_from_boosts(self) -> None:
        from app.rag.reinforcement import agent as agent_module

        entries = [
            {"feedback_id": "fb-rank", "feedback_type": "ranking",
             "results_used": json.dumps([{"symbol_id": "a::b", "helpful": True}]),
             "quality_rating": 4, "quality_score": 0.8},
            {"feedback_id": "fb-bug", "feedback_type": "tool_bug",
             "results_used": json.dumps([{"symbol_id": "c::d", "helpful": True}]),
             "quality_rating": 3, "quality_score": 0.7},
        ]
        boosted: list[str] = []
        marked: list[list[str]] = []

        class FakeAIStore:
            @staticmethod
            def get_unsynced_accepted_feedback(limit=50):
                return entries

            @staticmethod
            def extract_symbol_signals_from_feedback(feedback):
                return {r["results_used"] and "a::b": 0.5 for r in feedback if r["feedback_id"] == "fb-rank"}

            @staticmethod
            def mark_boost_applied(feedback_ids):
                marked.append(list(feedback_ids))

        class FakeFeedbackStore:
            @staticmethod
            def record_feedback(**kwargs):
                boosted.append(kwargs["symbol_id"])

        with patch.object(agent_module.ai_feedback_store, "get_unsynced_accepted_feedback", FakeAIStore.get_unsynced_accepted_feedback), \
             patch.object(agent_module.ai_feedback_store, "extract_symbol_signals_from_feedback", FakeAIStore.extract_symbol_signals_from_feedback), \
             patch.object(agent_module.ai_feedback_store, "mark_boost_applied", FakeAIStore.mark_boost_applied), \
             patch.object(agent_module.feedback_store, "record_feedback", FakeFeedbackStore.record_feedback), \
             patch.object(agent_module.ai_feedback_store, "evaluate_pending_feedback", lambda: {"evaluated": 0}), \
             patch.object(agent_module.ai_feedback_store, "get_feedback_stats", lambda: {"unconsumed_accepted": 0}):
            agent_module._agent_tick()

        self.assertEqual(boosted, ["a::b"], "tool_bug feedback must not produce boosts")
        self.assertEqual(sorted(marked[0]), ["fb-bug", "fb-rank"], "both entries still marked processed")


class ObservationsTestCase(unittest.TestCase):
    """Metal-detector extraction: TODOs, swallowed errors, stubs, dead code."""

    def _chunk(self, symbol_id, content, start, end, kind="fn"):
        from app.schemas.codebase import CodeChunk
        return CodeChunk(symbol_id=symbol_id, file_path="t.rs", kind=kind,
                         content=content, start_line=start, end_line=end)

    def test_todo_extraction(self):
        from app.rag.ingestion.observations import extract_observations
        src = "\n".join([
            "fn a() {}",
            "// TODO fix duplicate entries before settlement",
            "// normal comment",
            "fn b() {}",
        ])
        obs = extract_observations(src, "t.rs", [])
        todos = [o for o in obs if o.kind == "todo"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0].line, 2)
        self.assertIn("duplicate entries", todos[0].detail)

    def test_error_swallow(self):
        from app.rag.ingestion.observations import extract_observations
        src = "fn a() {\n    let _ = credit_wallet(&mut w, amt);\n    let x = 5;\n}\n"
        obs = extract_observations(src, "t.rs", [])
        kinds = [o.kind for o in obs]
        self.assertIn("error_swallow", kinds)
        sw = next(o for o in obs if o.kind == "error_swallow")
        self.assertIn("credit_wallet", sw.detail)

    def test_fail_open_on_auth(self):
        from app.rag.ingestion.observations import extract_observations
        src = "let token = headers.get(\"Authorization\").unwrap_or_default();\n"
        obs = extract_observations(src, "t.rs", [])
        self.assertTrue(any(o.kind == "fail_open" for o in obs))

    def test_fail_open_not_flagged_off_auth(self):
        from app.rag.ingestion.observations import extract_observations
        src = "let count = list.iter().count().unwrap_or_default();\n"
        obs = extract_observations(src, "t.rs", [])
        self.assertFalse(any(o.kind == "fail_open" for o in obs))

    def test_stub_function(self):
        from app.rag.ingestion.observations import extract_observations
        content = "\n".join([
            "fn settle_dispute(code: u32) {",
            "    println!(\"settling {}\", code);",
            "    info!(\"done\");",
            "}",
        ])
        chunk = self._chunk("m::settle_dispute", content, 1, 4)
        obs = extract_observations(content, "t.rs", [chunk])
        stubs = [o for o in obs if o.kind == "stub_fn"]
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0].symbol_id, "m::settle_dispute")

    def test_real_function_not_stub(self):
        from app.rag.ingestion.observations import extract_observations
        content = "\n".join([
            "fn settle(code: u32) -> Result<()> {",
            "    let tx = db.begin()?;",
            "    info!(\"settling\");",
            "    Ok(())",
            "}",
        ])
        chunk = self._chunk("m::settle", content, 1, 5)
        obs = extract_observations(content, "t.rs", [chunk])
        self.assertFalse(any(o.kind == "stub_fn" for o in obs))

    def test_commented_out_code(self):
        from app.rag.ingestion.observations import extract_observations
        src = "\n".join([
            "// if check_if_txn_exists(tx, id) {",
            "//     return Err(Duplicate);",
            "// }",
        ])
        obs = extract_observations(src, "t.rs", [])
        self.assertTrue(any(o.kind == "commented_code" for o in obs))

    def test_symbol_attachment(self):
        from app.rag.ingestion.observations import extract_observations
        src = "fn outer() {\n    // FIXME: this drifts\n    let _ = do_thing();\n}\n"
        chunk = self._chunk("m::outer", src, 1, 4)
        obs = extract_observations(src, "t.rs", [chunk])
        for o in obs:
            if o.kind in ("fixme", "error_swallow"):
                self.assertEqual(o.symbol_id, "m::outer")

    def test_error_swallow_await_chain(self):
        from app.rag.ingestion.observations import extract_observations
        src = "let _ = tx.commit().await;\nlet _ = client.send(req).await.map_err(log).await;\n"
        obs = extract_observations(src, "t.rs", [])
        self.assertEqual(len([o for o in obs if o.kind == "error_swallow"]), 2)

    def test_fail_open_context_lines(self):
        from app.rag.ingestion.observations import extract_observations
        src = "\n".join([
            "match headers.get(\"X-Auth-Token\") {",
            "    Some(v) => v,",
            "    None => default_token.clone().unwrap_or_default(),",
            "}",
        ])
        obs = extract_observations(src, "t.rs", [])
        self.assertTrue(any(o.kind == "fail_open" for o in obs))

    def test_prose_comment_not_flagged_as_code(self):
        from app.rag.ingestion.observations import extract_observations
        src = "\n".join([
            "// Resolve TLS certificates for both modes.",
            "// This is more efficient if the pool is warm.",
            "// Note: the logging for CA is common.",
        ])
        obs = extract_observations(src, "t.rs", [])
        self.assertFalse(any(o.kind == "commented_code" for o in obs))

    def test_generic_params_not_mangled_into_symbol_ids(self):
        from app.rag.ingestion.tree_sitter import extract_rust_chunks
        src = "\n".join([
            "pub async fn update_dispute_status<C: Client>(conn: &C, id: u32) -> Result<()> {",
            "    Ok(())",
            "}",
            "",
            "pub fn process<S>(s: S) -> S { s }",
        ])
        chunks = extract_rust_chunks("crates/wallet/queries.rs", src)
        ids = [c.symbol_id for c in chunks]
        self.assertIn("crates::wallet::queries::update_dispute_status", ids)
        self.assertIn("crates::wallet::queries::process", ids)
        self.assertFalse(any("_C" in i or "_S" in i for i in ids), f"mangled ids: {ids}")

    def test_hub_names_do_not_create_phantom_edges(self):
        from app.rag.indexing_service import _build_name_index, _extract_call_targets
        from app.schemas.codebase import CodeChunk
        chunks = [
            CodeChunk(symbol_id=f"m{i}::new", file_path=f"src/file{i}.rs", kind="fn",
                      content="fn new() -> Self", start_line=1, end_line=1)
            for i in range(10)
        ]
        name_index = _build_name_index(chunks)
        caller = CodeChunk(symbol_id="x::caller", file_path="src/other.rs", kind="fn",
                           content="let c = new();", start_line=1, end_line=1)
        self.assertEqual(_extract_call_targets(caller, name_index), [],
                         "hub name 'new' with 10 candidates must not resolve cross-file")

    def test_scoped_resolution_prefers_same_file(self):
        from app.rag.indexing_service import _build_name_index, _extract_call_targets
        from app.schemas.codebase import CodeChunk
        chunks = [
            CodeChunk(symbol_id="m::process", file_path="src/a.rs", kind="fn",
                      content="fn process() {}", start_line=1, end_line=1),
            CodeChunk(symbol_id="n::process", file_path="src/b.rs", kind="fn",
                      content="fn process() {}", start_line=1, end_line=1),
        ]
        name_index = _build_name_index(chunks)
        caller = CodeChunk(symbol_id="x::caller", file_path="src/a.rs", kind="fn",
                           content="process();", start_line=1, end_line=1)
        calls = _extract_call_targets(caller, name_index)
        self.assertEqual(calls, ["m::process"], "same-file candidate must win")

    def test_qualified_call_resolves_precisely(self):
        from app.rag.indexing_service import _build_name_index, _build_path_index, _extract_call_targets
        from app.schemas.codebase import CodeChunk
        chunks = [
            CodeChunk(symbol_id="inv::InventoryClient::new", file_path="src/inv.rs", kind="fn",
                      content="fn new() {}", start_line=1, end_line=1),
        ] + [
            CodeChunk(symbol_id=f"m{i}::new", file_path=f"src/f{i}.rs", kind="fn",
                      content="fn new() {}", start_line=1, end_line=1)
            for i in range(8)
        ]
        name_index = _build_name_index(chunks)
        path_index = _build_path_index(chunks)
        caller = CodeChunk(symbol_id="h::handler", file_path="src/h.rs", kind="fn",
                           content="let c = InventoryClient::new();", start_line=1, end_line=1)
        calls = _extract_call_targets(caller, name_index, path_index)
        self.assertIn("inv::InventoryClient::new", calls)
        self.assertEqual(len(calls), 1, f"qualified call must resolve to exactly one target, got {calls}")

    def test_diff_scoped_resolution(self):
        from app.rag.diff_parser import resolve_diff_symbols

        class FakeGraph:
            def search_symbols(self, name, limit=20):
                # Two same-named symbols in different modules — the phantom case
                return [
                    {"symbol_id": "crates::wallet::queries::fetch_uam_users_map"},
                    {"symbol_id": "dashboard::admin::fetch_uam_users_map"},
                    {"symbol_id": "fastag::reports::fetch_uam_users_map"},
                ][:limit]

        diff = "\n".join([
            "--- a/crates/wallet/queries.rs",
            "+++ b/crates/wallet/queries.rs",
            "@@ -10,3 +10,4 @@",
            "-fn fetch_uam_users_map() -> Map {",
            "+fn fetch_uam_users_map() -> HashMap<String, User> {",
            "     let m = load();",
            " }",
        ])
        resolved = resolve_diff_symbols(diff, FakeGraph())
        ids = [s["symbol_id"] for s in resolved["changed_symbols"]]
        self.assertEqual(ids, ["crates::wallet::queries::fetch_uam_users_map"],
                         "must resolve only within the diff's modules")


class ContractDriftTestCase(unittest.TestCase):
    """Pin the interfaces between components so they cannot drift silently.
    Every test here corresponds to a real outage from 2026-10-02."""

    def test_make_decision_schema_state_cap_matches_code(self):
        """The schema advertised 8000-char states while code enforced 2000 —
        every spec-following client was rejected."""
        from app.mcp import readonly_server
        import re

        schema = readonly_server._TOOLS["make_decision"]["schema"]
        m = re.search(r"max (\d+) chars", schema["properties"]["state"]["description"])
        self.assertIsNotNone(m, "state description must state the cap")
        from app.core import systemone
        self.assertEqual(int(m.group(1)), systemone.MAX_STATE_CHARS,
                         "advertised cap must equal enforced cap")

    def test_docstring_state_cap_matches_code(self):
        from app.mcp.tools.graph_read_tools import make_decision
        from app.core import systemone
        import re

        doc = make_decision.__doc__ or ""
        m = re.search(r"max (\d+) chars", doc)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), systemone.MAX_STATE_CHARS)

    def test_feedback_stats_keys_cover_tick_requirements(self):
        """get_feedback_stats never returned total_feedback; the tick
        KeyError'd on it every cycle for months, invisibly."""
        import inspect
        import re

        from app.rag.reinforcement import agent, ai_feedback_store

        source = inspect.getsource(ai_feedback_store.get_feedback_stats)
        tick_source = inspect.getsource(agent)
        # keys the tick reads
        required = {"total_feedback"}
        m = re.search(r"fb_stats\.get\(\"(\w+)\", 0\)|fb_stats\[\"(\w+)\"\]", tick_source)
        keys_used = set(re.findall(r"fb_stats(?:\.get)?\[?\"(\w+)\"", tick_source)) | required
        # keys the store returns
        returned = set(re.findall(r"\"(\w+)\":", source))
        missing = keys_used - returned - {"get"}
        self.assertFalse(missing, f"tick reads keys the store never returns: {missing}")

    def test_deployer_guard_matches_indexing_log_lines(self):
        """The deployer's indexing-deferral grep matched NONE of the actual
        log lines — a mid-index deploy would have killed a 1.8h build."""
        import re
        from pathlib import Path

        deployer = Path("/app/scripts/auto-deploy.sh")
        if not deployer.exists():
            self.skipTest("scripts/ not mounted (running outside run-tests.sh)")
        deployer_text = deployer.read_text()
        m = re.search(r'grep -qE "([^"]+)"', deployer_text)
        self.assertIsNotNone(m, "deployer must have an indexing guard")
        guard = re.compile(m.group(1))

        sources = {
            Path("/app/app/rag/indexing_service.py").read_text(),
            Path("/app/app/rag/retrieval/weaviate_semantic.py").read_text(),
        }
        # every logged message the guard relies on must exist in the source
        for literal in ("Embedded and inserted", "Rebuilding semantic index", "Building new graph"):
            self.assertTrue(any(literal in src for src in sources),
                            f"indexing no longer logs '{literal}' — guard is stale")
            self.assertTrue(guard.search(literal), f"guard regex does not match '{literal}'")

    def test_negative_asymmetry_counters_entrenchment(self):
        from app.rag.reinforcement.feedback_store import compute_boost_weight
        # 19 accumulated positives vs 1 correction: k=1 kept it at 0.72 (entrenched)
        w_k2 = compute_boost_weight(19, 1)
        self.assertLess(w_k2, 19 / 25, "one correction must dent the k=1 entrenchment (0.72)")
        # a fresh symbol with one negative goes clearly negative
        self.assertLess(compute_boost_weight(0, 1), 0)
        # with k=2 the neutral point is p = 2n, not p = n
        self.assertAlmostEqual(compute_boost_weight(10, 5), 0.0)

    def test_pseudo_symbols_rejected_from_feedback(self):
        from unittest.mock import patch

        from app.rag.reinforcement import feedback_store

        def _boom(*a, **k):
            raise AssertionError("store must not be touched for pseudo-symbols")

        with patch.object(feedback_store, "_ensure_schema", _boom), \
                patch.object(feedback_store, "_connect", _boom):
            feedback_store.record_feedback("q", "fastag::netc_handlers::module_exports", 0.5, 1)
            feedback_store.record_feedback("q", "x::y::file_summary", 0.5, 1)

    def test_diff_deleted_symbols_scoped_to_diff(self):
        from app.rag.diff_parser import resolve_diff_symbols

        class FakeGraph:
            def search_symbols(self, name, limit=20):
                if name != "fetch_uam_users_map":
                    return []
                return [
                    {"symbol_id": "crates::wallet::queries::fetch_uam_users_map"},
                    {"symbol_id": "dashboard::admin::fetch_uam_users_map"},
                ][:limit]

        diff = "\n".join([
            "--- a/crates/wallet/queries.rs",
            "+++ b/crates/wallet/queries.rs",
            "@@ -10,2 +10,1 @@",
            "-fn fetch_uam_users_map() -> Map {",
            "-fn gone_helper() {}",
        ])
        resolved = resolve_diff_symbols(diff, FakeGraph())
        ids = [s["symbol_id"] for s in resolved["deleted_symbols"]]
        self.assertEqual(ids, ["crates::wallet::queries::fetch_uam_users_map"],
                         "deleted symbols must resolve only within the diff's modules — "
                         "global resolution invented deletions in files not in the diff")

    def test_risk_markers_unsafe_ffi_panic(self):
        from app.rag.ingestion.observations import extract_observations
        src = "\n".join([
            "fn raw() {",
            "    unsafe { std::ptr::write(p, 1); }",
            "}",
            "extern \"C\" {",
            "    fn ext_call(x: i32) -> i32;",
            "}",
            "fn boom() {",
            "    panic!(\"unreachable state\");",
            "}",
        ])
        obs = extract_observations(src, "t.rs", [])
        kinds = {o.kind for o in obs}
        self.assertIn("unsafe_block", kinds)
        self.assertIn("ffi_boundary", kinds)
        self.assertIn("panic_path", kinds)

    def test_unwrap_density_flagged(self):
        from app.schemas.codebase import CodeChunk
        from app.rag.ingestion.observations import extract_observations
        body = ["fn risky() {"] + [f"    let v{x} = opt{x}.unwrap();" for x in range(6)] + ["}"]
        content = "\n".join(body)
        chunk = CodeChunk(symbol_id="m::risky", file_path="t.rs", kind="fn",
                          content=content, start_line=1, end_line=len(body))
        obs = extract_observations(content, "t.rs", [chunk])
        dense = [o for o in obs if o.kind == "unwrap_density"]
        self.assertEqual(len(dense), 1)
        self.assertIn("6 unwrap", dense[0].detail)

    def test_block_comment_fns_not_indexed(self):
        from app.rag.ingestion.tree_sitter import extract_rust_chunks
        src = "\n".join([
            "/*",
            "fn is_eligible_for_post_dated_cb() -> bool {",
            "    false // old version, dead",
            "}",
            "*/",
            "fn is_eligible_for_post_dated_cb() -> bool {",
            "    true",
            "}",
        ])
        chunks = extract_rust_chunks("crates/x.rs", src)
        fns = [c for c in chunks if c.symbol_id.endswith("is_eligible_for_post_dated_cb")]
        self.assertEqual(len(fns), 1, "fn inside /* */ must not be indexed as live")

    def test_block_comment_dead_code_observed(self):
        from app.rag.ingestion.observations import extract_observations
        src = "/*\nfn old_fn(x: u32) -> u32 {\n    let y = x + 1;\n    y\n}\n*/\nfn live() {}"
        obs = extract_observations(src, "t.rs", [])
        blocks = [o for o in obs if o.kind == "commented_code" and "/*" in o.detail or "dead code" in o.detail]
        self.assertTrue(any("dead code" in (o.detail or "") for o in obs), f"expected dead-code observation, got {[o.kind for o in obs]}")

    def test_in_body_modification_via_hunk_header(self):
        from app.rag.diff_parser import extract_symbols_from_diff
        diff = "\n".join([
            "--- a/crates/wallet/core.rs",
            "+++ b/crates/wallet/core.rs",
            "@@ -40,6 +40,7 @@ fn settle_transaction",
            "     let tx = begin();",
            "+    let guard = acquire_lock();",
            "     Ok(())",
        ])
        extraction = extract_symbols_from_diff(diff)
        self.assertIn("settle_transaction", extraction["modified_symbols"],
                      "in-body changes must attribute to the enclosing fn from the hunk header")

    def test_is_test_detection(self):
        from app.rag.ingestion.tree_sitter import extract_rust_chunks
        src = "\n".join([
            "#[cfg(test)]",
            "mod tests {",
            "    use super::*;",
            "    #[test]",
            "    fn settles_ok() { assert!(true); }",
            "    #[tokio::test]",
            "    async fn async_path() { assert!(true); }",
            "}",
            "fn production_fn() -> u32 { 1 }",
        ])
        chunks = {c.symbol_id.split("::")[-1]: c for c in extract_rust_chunks("crates/wallet.rs", src)}
        self.assertTrue(chunks["settles_ok"].is_test, "#[test] inside cfg(test) mod must be marked")
        self.assertTrue(chunks["async_path"].is_test, "#[tokio::test] must be marked")
        self.assertTrue(chunks["production_fn"] is not None and not chunks["production_fn"].is_test,
                        "production fn must NOT be marked")

    def test_is_test_standalone_attr(self):
        from app.rag.ingestion.tree_sitter import extract_rust_chunks
        src = "\n".join([
            "/// doc for real fn",
            "fn process_payment() {}",
            "#[test]",
            "fn direct_test() { assert_eq!(1, 1); }",
        ])
        chunks = {c.symbol_id.split("::")[-1]: c for c in extract_rust_chunks("crates/pay.rs", src)}
        self.assertTrue(chunks["direct_test"].is_test, "standalone #[test] fn must be marked")
        self.assertFalse(chunks["process_payment"].is_test, "preceding fn must not inherit the flag")

    def test_get_tests_for_symbol(self):
        from app.rag.retrieval.graph import graph_index
        graph_index.upsert_symbol("crates::x::pay", metadata={"kind": "fn", "file_path": "crates/x.rs"})
        graph_index.upsert_symbol(
            "crates::x::tests::t1", calls=["crates::x::pay"],
            metadata={"kind": "fn", "file_path": "crates/x.rs", "is_test": True, "start_line": 5, "end_line": 7},
        )
        graph_index.upsert_symbol(
            "crates::x::tests::t2", uses=["crates::x::pay"],
            metadata={"kind": "fn", "file_path": "crates/x.rs", "is_test": True, "start_line": 9, "end_line": 11},
        )
        graph_index.upsert_symbol(
            "crates::x::prod_caller", calls=["crates::x::pay"],
            metadata={"kind": "fn", "file_path": "crates/x.rs"},
        )
        from app.mcp.tools import graph_read_tools as grt
        out = grt.get_tests_for_symbol("crates::x::pay")
        self.assertEqual(out["test_count"], 2)
        self.assertEqual(out["tests_calling"][0]["symbol_id"], "crates::x::tests::t1")
        self.assertEqual(out["tests_referencing_type"][0]["relation"], "type_reference")
        self.assertTrue(out["test_files"])
        # production caller must not appear in test results
        self.assertNotIn("crates::x::prod_caller", str(out["tests_calling"]))

    def test_risk_score_excludes_tests(self):
        from app.rag.retrieval.graph import graph_index
        graph_index.upsert_symbol("crates::x::pay", metadata={"kind": "fn"})
        for i in range(12):  # 12 test callers would be 'medium' alone
            graph_index.upsert_symbol(
                f"crates::x::tests::t{i}", calls=["crates::x::pay"],
                metadata={"kind": "fn", "is_test": True},
            )
        from app.mcp.tools import graph_read_tools as grt
        grt._TEST_SYMBOLS_CACHE = (0.0, set())
        out = grt.get_blast_radius("crates::x::pay")
        self.assertEqual(out["test_caller_count"], 12)
        self.assertEqual(out["risk_score"], "low", "12 test-only callers must not inflate production risk")

    def test_recent_changes_near(self):
        from unittest.mock import patch
        from app.rag.retrieval.graph import graph_index
        from types import SimpleNamespace
        graph_index.upsert_symbol("crates::x::pay", metadata={"kind": "fn", "file_path": "crates/x/pay.rs"})
        graph_index.upsert_symbol("crates::x::caller", calls=["crates::x::pay"], metadata={"kind": "fn", "file_path": "crates/x/caller.rs"})
        snap = SimpleNamespace(chunks=[
            SimpleNamespace(symbol_id="crates::x::pay", file_path="crates/x/pay.rs", start_line=1, end_line=10, is_test=False, kind="fn"),
            SimpleNamespace(symbol_id="crates::x::caller", file_path="crates/x/caller.rs", start_line=1, end_line=5, is_test=False, kind="fn"),
        ])
        fake_history = [
            {"hash": "a" * 40, "date": "2026-10-01T10:00:00 +0000", "author": "dev1", "subject": "fix pay", "files": ["crates/x/pay.rs"]},
            {"hash": "b" * 40, "date": "2026-09-28T10:00:00 +0000", "author": "dev2", "subject": "tune caller", "files": ["crates/x/caller.rs"]},
            {"hash": "c" * 40, "date": "2026-09-20T10:00:00 +0000", "author": "dev3", "subject": "unrelated", "files": ["crates/other.rs"]},
        ]
        from app.mcp.tools import graph_read_tools as grt
        with patch("app.rag.ingestion.git_ingestor.get_recent_history", return_value=fake_history), \
             patch("app.core.index_store.index_metadata_store.load_snapshot", return_value=snap):
            out = grt.recent_changes_near("crates::x::pay", days=30)
        self.assertEqual(out["direct_commit_count"], 1)
        self.assertEqual(out["direct_commits"][0]["subject"], "fix pay")
        self.assertEqual(out["blast_radius_commit_count"], 1, "caller file commit must surface via blast radius")
        self.assertEqual(out["blast_radius_commits"][0]["subject"], "tune caller")

    def test_find_hotspots(self):
        from unittest.mock import patch
        from app.rag.retrieval.graph import graph_index
        from types import SimpleNamespace
        # churny, connected, untested -> hotspot
        graph_index.upsert_symbol("crates::x::hot", metadata={"kind": "fn", "file_path": "crates/x/hot.rs"})
        for i in range(8):
            graph_index.upsert_symbol(f"crates::x::c{i}", calls=["crates::x::hot"], metadata={"kind": "fn", "file_path": "crates/x/other.rs"})
        # churny but trivial, no symbols
        snap = SimpleNamespace(chunks=[
            SimpleNamespace(symbol_id="crates::x::hot", file_path="crates/x/hot.rs", start_line=1, end_line=20, is_test=False, kind="fn"),
        ])
        fake_churn = {"crates/x/hot.rs": 9, "crates/x/empty.rs": 50}
        from app.mcp.tools import graph_read_tools as grt
        with patch("app.rag.ingestion.git_ingestor.get_file_churn", return_value=fake_churn), \
             patch("app.core.index_store.index_metadata_store.load_snapshot", return_value=snap):
            out = grt.find_hotspots(days=30)
        self.assertEqual(out["hotspot_count"], 1, "empty.rs has churn but no indexed symbols")
        h = out["hotspots"][0]
        self.assertEqual(h["file_path"], "crates/x/hot.rs")
        self.assertEqual(h["components"]["commits"], 9)
        self.assertEqual(h["anchor_symbol"]["test_callers"], 0)
        self.assertGreater(h["score"], 9, "untested + connected must amplify beyond raw churn")

    def test_golden_eval_scoring(self):
        from unittest.mock import patch
        from app.rag.retrieval.graph import graph_index
        graph_index.upsert_symbol("crates::x::pay", metadata={"kind": "fn", "file_path": "crates/x.rs"})
        graph_index.upsert_symbol("crates::x::other", metadata={"kind": "fn", "file_path": "crates/x.rs"})
        from app.rag.reinforcement import golden_eval as ge

        def fake_pairs():
            return [
                {"query": "payment flow", "symbol_id": "crates::x::pay"},
                {"query": "other thing", "symbol_id": "crates::x::pay"},
            ]

        def fake_search(query, limit=10):
            results = [{"symbol_id": "crates::x::pay", "score": 0.9}]
            if query == "other thing":
                results = [{"symbol_id": "crates::x::other", "score": 0.8}]  # miss
            return {"results": results}

        with patch.object(ge, "build_golden_set", fake_pairs), \
             patch("app.mcp.tools.graph_read_tools.semantic_search", fake_search):
            out = ge.run_eval()
        self.assertEqual(out["golden_pairs"], 2)
        self.assertEqual(out["hit_at_5"], 0.5)
        self.assertEqual(out["hit_at_10"], 0.5)
        self.assertAlmostEqual(out["mrr"], 0.5)  # rank-1 hit + one miss
        self.assertEqual(out["miss_count"], 1)
        self.assertEqual(out["misses"][0]["query"], "other thing")
