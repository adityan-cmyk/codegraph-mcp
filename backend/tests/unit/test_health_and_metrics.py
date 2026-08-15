import sys
import unittest
from unittest.mock import patch, MagicMock

from app.core.health import (
    BackendHealthResult,
    check_postgres,
    check_redis,
    check_weaviate,
    check_neo4j,
    check_all_backends,
)
from app.core.metrics import MetricsCollector


class BackendHealthTestCase(unittest.TestCase):
    @patch("app.core.health.settings")
    def test_check_postgres_disabled(self, mock_settings) -> None:
        mock_settings.incident_store_backend = "memory"
        result = check_postgres()
        self.assertEqual(result.status, "disabled")

    @patch("app.core.health.settings")
    def test_check_postgres_healthy(self, mock_settings) -> None:
        mock_settings.incident_store_backend = "postgres"
        mock_settings.postgres_dsn = "postgresql://oncall:oncall@localhost:5432/oncall"

        mock_psycopg = MagicMock()
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = {"ok": 1}
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_psycopg.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
        mock_psycopg.rows.dict_row = MagicMock()

        with patch.dict(sys.modules, {"psycopg": mock_psycopg, "psycopg.rows": mock_psycopg.rows}):
            result = check_postgres()
            self.assertEqual(result.status, "healthy")

    @patch("app.core.health.settings")
    def test_check_postgres_unhealthy(self, mock_settings) -> None:
        mock_settings.incident_store_backend = "postgres"
        mock_settings.postgres_dsn = "postgresql://bad:bad@localhost:5432/bad"

        mock_psycopg = MagicMock()
        mock_psycopg.connect.side_effect = Exception("Connection refused")

        with patch.dict(sys.modules, {"psycopg": mock_psycopg, "psycopg.rows": MagicMock()}):
            result = check_postgres()
            self.assertEqual(result.status, "unhealthy")
            self.assertIn("Connection refused", result.detail)

    @patch("app.core.health.settings")
    def test_check_redis_healthy(self, mock_settings) -> None:
        mock_settings.redis_url = "redis://localhost:6379/0"

        mock_redis = MagicMock()
        mock_client = MagicMock()
        mock_client.ping.return_value = True
        mock_redis.from_url.return_value = mock_client

        with patch.dict(sys.modules, {"redis": mock_redis}):
            result = check_redis()
            self.assertEqual(result.status, "healthy")

    @patch("app.core.health.settings")
    def test_check_redis_unhealthy(self, mock_settings) -> None:
        mock_settings.redis_url = "redis://localhost:6379/0"

        mock_redis = MagicMock()
        mock_redis.from_url.side_effect = Exception("Connection refused")

        with patch.dict(sys.modules, {"redis": mock_redis}):
            result = check_redis()
            self.assertEqual(result.status, "unhealthy")

    @patch("app.core.health.settings")
    def test_check_weaviate_disabled(self, mock_settings) -> None:
        mock_settings.semantic_index_backend = "memory"
        result = check_weaviate()
        self.assertEqual(result.status, "disabled")

    @patch("app.core.health.settings")
    def test_check_neo4j_disabled(self, mock_settings) -> None:
        mock_settings.graph_index_backend = "memory"
        result = check_neo4j()
        self.assertEqual(result.status, "disabled")

    def test_check_all_backends_returns_structure(self) -> None:
        result = check_all_backends()
        self.assertIn("status", result)
        self.assertIn("backends", result)
        self.assertEqual(len(result["backends"]), 4)

    def test_check_all_backends_healthy_when_all_disabled(self) -> None:
        with patch("app.core.health.check_postgres", return_value=BackendHealthResult("postgres", "disabled")):
            with patch("app.core.health.check_redis", return_value=BackendHealthResult("redis", "healthy")):
                with patch("app.core.health.check_weaviate", return_value=BackendHealthResult("weaviate", "disabled")):
                    with patch("app.core.health.check_neo4j", return_value=BackendHealthResult("neo4j", "disabled")):
                        result = check_all_backends()
                        self.assertEqual(result["status"], "healthy")

    def test_check_all_backends_degraded_when_unhealthy(self) -> None:
        with patch("app.core.health.check_postgres", return_value=BackendHealthResult("postgres", "unhealthy", "conn refused")):
            with patch("app.core.health.check_redis", return_value=BackendHealthResult("redis", "healthy")):
                with patch("app.core.health.check_weaviate", return_value=BackendHealthResult("weaviate", "disabled")):
                    with patch("app.core.health.check_neo4j", return_value=BackendHealthResult("neo4j", "disabled")):
                        result = check_all_backends()
                        self.assertEqual(result["status"], "degraded")


class MetricsCollectorTestCase(unittest.TestCase):
    def test_initial_snapshot(self) -> None:
        collector = MetricsCollector()
        snapshot = collector.snapshot()
        self.assertEqual(snapshot["incidents"]["created"], 0)
        self.assertEqual(snapshot["incidents"]["resolved"], 0)
        self.assertEqual(snapshot["incidents"]["failed"], 0)
        self.assertEqual(snapshot["analysis"]["total"], 0)
        self.assertEqual(snapshot["cache"]["hits"], 0)
        self.assertEqual(snapshot["cache"]["misses"], 0)
        self.assertGreaterEqual(snapshot["uptime_seconds"], 0)

    def test_record_incident_lifecycle(self) -> None:
        collector = MetricsCollector()
        collector.record_incident_created()
        collector.record_incident_resolved()
        collector.record_incident_failed()

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["incidents"]["created"], 1)
        self.assertEqual(snapshot["incidents"]["resolved"], 1)
        self.assertEqual(snapshot["incidents"]["failed"], 1)

    def test_record_analysis_latency(self) -> None:
        collector = MetricsCollector()
        collector.record_analysis(1.5)
        collector.record_analysis(2.0)
        collector.record_analysis(0.5)

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["analysis"]["total"], 3)
        self.assertAlmostEqual(snapshot["analysis"]["avg_latency_seconds"], 1.333, places=2)

    def test_cache_hit_rate(self) -> None:
        collector = MetricsCollector()
        for _ in range(7):
            collector.record_cache_hit()
        for _ in range(3):
            collector.record_cache_miss()

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["cache"]["hits"], 7)
        self.assertEqual(snapshot["cache"]["misses"], 3)
        self.assertAlmostEqual(snapshot["cache"]["hit_rate"], 0.7, places=2)

    def test_kb_sync_metrics(self) -> None:
        collector = MetricsCollector()
        collector.record_kb_sync(success=True)
        collector.record_kb_sync(success=True)
        collector.record_kb_sync(success=False)

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["kb_sync"]["total"], 3)
        self.assertEqual(snapshot["kb_sync"]["failed"], 1)

    def test_mcp_metrics(self) -> None:
        collector = MetricsCollector()
        collector.record_mcp_tool_call(error=False)
        collector.record_mcp_tool_call(error=True)

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["mcp"]["tool_calls"], 2)
        self.assertEqual(snapshot["mcp"]["tool_errors"], 1)

    def test_reset_clears_all(self) -> None:
        collector = MetricsCollector()
        collector.record_incident_created()
        collector.record_analysis(1.0)
        collector.record_cache_hit()

        collector.reset()

        snapshot = collector.snapshot()
        self.assertEqual(snapshot["incidents"]["created"], 0)
        self.assertEqual(snapshot["analysis"]["total"], 0)
        self.assertEqual(snapshot["cache"]["hits"], 0)

    def test_latency_sliding_window(self) -> None:
        collector = MetricsCollector()
        for i in range(1200):
            collector.record_analysis(float(i))

        snapshot = collector.snapshot()
        self.assertLessEqual(snapshot["analysis"]["total"], 1200)


if __name__ == "__main__":
    unittest.main()
