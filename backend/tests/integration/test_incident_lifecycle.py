import os
import unittest

from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.incident_store import incident_session_store
from app.main import app


@unittest.skipUnless(
    os.environ.get("TEST_POSTGRES_DSN"),
    "Set TEST_POSTGRES_DSN to run Postgres integration tests",
)
class PostgresIncidentLifecycleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_backend = settings.incident_store_backend
        settings.incident_store_backend = "postgres"
        settings.postgres_dsn = os.environ["TEST_POSTGRES_DSN"]

        from app.core.database.postgres import get_postgres_incident_repository
        cls.pg_store = get_postgres_incident_repository()

        import app.core.incident_store as store_module
        store_module.incident_session_store = cls.pg_store
        cls._patched_store_module = store_module

        import app.core.incident_service as svc_module
        svc_module.incident_session_store = cls.pg_store

        import app.core.timeline_service as tl_module
        tl_module.incident_session_store = cls.pg_store

    @classmethod
    def tearDownClass(cls) -> None:
        settings.incident_store_backend = cls.original_backend
        from app.core.repositories.in_memory_incident_repository import InMemoryIncidentRepository
        fresh_memory = InMemoryIncidentRepository()
        cls._patched_store_module.incident_session_store = fresh_memory

        import app.core.incident_service as svc_module
        svc_module.incident_session_store = fresh_memory

        import app.core.timeline_service as tl_module
        tl_module.incident_session_store = fresh_memory

    def setUp(self) -> None:
        self.pg_store.reset()
        self.client = TestClient(app)
        self.payload = {
            "fingerprint": {
                "service": "pg-test-service",
                "panic_type": "runtime_error",
                "top_frame": "src/pg_test.rs:10",
                "commit_hash": "pg123",
            },
            "environment": "PROD",
            "build_id": "pg-build-1",
            "raw_log": "runtime error in pg_test",
            "source": "integration_test",
        }

    def test_create_and_retrieve_incident(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()

        self.assertEqual(created["state"], "CREATED")
        self.assertEqual(created["fingerprint"]["service"], "pg-test-service")

        retrieved = self.client.get(f"/api/incidents/{created['session_id']}").json()
        self.assertEqual(retrieved["session_id"], created["session_id"])
        self.assertEqual(len(retrieved["timeline"]), 1)

    def test_incident_persists_across_store_instances(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()
        session_id = created["session_id"]

        from app.core.database.postgres import get_postgres_incident_repository
        fresh_store = get_postgres_incident_repository()

        retrieved = fresh_store.get_session(session_id)
        self.assertEqual(retrieved.session_id, session_id)
        self.assertEqual(retrieved.fingerprint.service, "pg-test-service")

    def test_full_lifecycle_creates_and_resolves(self) -> None:
        from unittest.mock import patch
        from app.schemas.incident import IncidentAnalysis
        from app.schemas.telemetry import ConfidenceScore

        with patch("app.core.incident_service.run_incident_workflow") as mock_workflow:
            mock_workflow.return_value = IncidentAnalysis(
                root_cause="Integration root cause",
                patch="// integration patch",
                confidence=[ConfidenceScore(label="Test", value="0.90")],
                context={"primary_symbol": "pg::test"},
            )

            created = self.client.post("/api/incidents/", json=self.payload).json()

            analysis = self.client.post(
                f"/api/incidents/{created['session_id']}/analyze", json={}
            ).json()
            self.assertEqual(analysis["session"]["state"], "VALIDATING")

        states = ["RESOLVED"]
        for next_state in states:
            result = self.client.post(
                f"/api/incidents/{created['session_id']}/state",
                json={
                    "next_state": next_state,
                    "event_type": "test_resolve",
                    "payload": {"actor": "integration_test"},
                },
            )
            self.assertEqual(result.status_code, 200)

        session = self.client.get(f"/api/incidents/{created['session_id']}").json()
        self.assertEqual(session["state"], "RESOLVED")

    def test_list_incidents_returns_all(self) -> None:
        self.client.post("/api/incidents/", json=self.payload)
        self.client.post("/api/incidents/", json={
            **self.payload,
            "raw_log": "second incident",
        })

        response = self.client.get("/api/incidents/").json()
        self.assertEqual(len(response), 2)

    def test_timeline_events_persist(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()
        session_id = created["session_id"]

        self.client.post(
            f"/api/incidents/{session_id}/chat",
            json={"role": "user", "content": "What happened?"},
        )

        from app.core.database.postgres import get_postgres_incident_repository
        fresh_store = get_postgres_incident_repository()
        session = fresh_store.get_session(session_id)
        chat_events = [e for e in session.timeline if e.event_type == "chat_message"]
        self.assertEqual(len(chat_events), 1)
        self.assertEqual(chat_events[0].payload["content"], "What happened?")


@unittest.skipUnless(
    os.environ.get("TEST_POSTGRES_DSN"),
    "Set TEST_POSTGRES_DSN to run Postgres integration tests",
)
class PostgresHealthCheckTestCase(unittest.TestCase):
    def test_health_endpoint_reports_postgres_healthy(self) -> None:
        client = TestClient(app)
        response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        pg_check = next((b for b in body["backends"] if b["backend"] == "postgres"), None)
        self.assertIsNotNone(pg_check)
        self.assertIn(pg_check["status"], ["healthy", "disabled"])


class MetricsEndpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        from app.core.metrics import metrics_collector
        metrics_collector.reset()
        self.client = TestClient(app)

    def test_metrics_endpoint_returns_structure(self) -> None:
        response = self.client.get("/api/metrics")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("incidents", body)
        self.assertIn("analysis", body)
        self.assertIn("cache", body)
        self.assertIn("chat", body)
        self.assertIn("mcp", body)
        self.assertIn("kb_sync", body)
        self.assertIn("uptime_seconds", body)

    def test_metrics_tracks_incident_creation(self) -> None:
        incident_session_store.reset()
        self.client.post("/api/incidents/", json={
            "fingerprint": {
                "service": "metrics-service",
                "panic_type": "panic",
                "top_frame": "src/metrics.rs:1",
                "commit_hash": "met123",
            },
            "environment": "UAT",
            "build_id": "metrics-build",
            "raw_log": "metrics panic",
            "source": "test",
        })

        metrics = self.client.get("/api/metrics").json()
        self.assertGreaterEqual(metrics["incidents"]["created"], 1)


if __name__ == "__main__":
    unittest.main()
