import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.index_store import index_metadata_store
from app.main import app
from app.rag.ingestion.tree_sitter import extract_rust_chunks, generate_symbol_id
from app.rag.indexing_service import index_rust_repository, replay_indexes_from_storage
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


if __name__ == "__main__":
    unittest.main()
