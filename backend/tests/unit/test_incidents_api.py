import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agents.client import _chat_completions_path, _extract_json_payload, hosted_model_client
from app.core.incident_store import incident_session_store
from app.main import app
from app.schemas.incident import IncidentAnalysis
from app.schemas.telemetry import ConfidenceScore


class IncidentApiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        incident_session_store.reset()
        self.client = TestClient(app)
        hosted_model_client._client = None
        hosted_model_client._path = None
        self.payload = {
            "fingerprint": {
                "service": "auth-service",
                "panic_type": "panic",
                "top_frame": "src/auth.rs:42",
                "commit_hash": "abc1234",
            },
            "environment": "UAT",
            "build_id": "build-42",
            "raw_log": "thread panicked at src/auth.rs:42",
            "source": "manual",
        }

    def test_create_incident_returns_created_session(self) -> None:
        response = self.client.post("/api/incidents/", json=self.payload)

        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["state"], "CREATED")
        self.assertEqual(body["fingerprint"]["service"], "auth-service")
        self.assertEqual(len(body["timeline"]), 1)

    def test_valid_transition_updates_state(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()

        response = self.client.post(
            f"/api/incidents/{created['session_id']}/state",
            json={"next_state": "INGESTING", "event_type": "ingest_started"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "INGESTING")
        self.assertEqual(len(response.json()["timeline"]), 2)

    def test_invalid_transition_returns_conflict(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()

        response = self.client.post(
            f"/api/incidents/{created['session_id']}/state",
            json={"next_state": "RESOLVED"},
        )

        self.assertEqual(response.status_code, 409)

    def test_websocket_receives_initial_session_snapshot(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()

        with self.client.websocket_connect(
            f"/ws/incident/{created['session_id']}",
            headers={"origin": "http://localhost:5173"},
        ) as websocket:
            event = websocket.receive_json()

        self.assertEqual(event["type"], "session_snapshot")
        self.assertEqual(event["session"]["session_id"], created["session_id"])

    def test_cors_preflight_allows_configured_origin(self) -> None:
        response = self.client.options(
            "/api/incidents/",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_cors_preflight_blocks_untrusted_origin(self) -> None:
        response = self.client.options(
            "/api/incidents/",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIsNone(response.headers.get("access-control-allow-origin"))

    def test_trusted_host_middleware_rejects_unknown_host(self) -> None:
        response = self.client.get("/health", headers={"host": "evil.example"})

        self.assertEqual(response.status_code, 400)

    def test_create_incident_rejects_blank_log(self) -> None:
        invalid_payload = {**self.payload, "raw_log": ""}

        response = self.client.post("/api/incidents/", json=invalid_payload)

        self.assertEqual(response.status_code, 422)

    @patch("app.core.incident_service.run_incident_workflow")
    def test_analyze_incident_returns_analysis_and_moves_to_validating(self, run_incident_workflow) -> None:
        run_incident_workflow.return_value = IncidentAnalysis(
            root_cause="Mocked root cause",
            patch="// mocked patch",
            confidence=[
                ConfidenceScore(label="Retrieval", value="0.80"),
                ConfidenceScore(label="Graph", value="0.70"),
            ],
            context={
                "source": "test",
                "primary_symbol": "auth::login_user",
                "indexed_symbols": [{"symbol_id": "auth::login_user"}],
            },
        )
        created = self.client.post("/api/incidents/", json=self.payload).json()

        response = self.client.post(f"/api/incidents/{created['session_id']}/analyze", json={})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["session"]["state"], "VALIDATING")
        self.assertEqual(body["analysis"]["root_cause"], "Mocked root cause")
        self.assertEqual(body["session"]["timeline"][-1]["payload"]["primary_symbol"], "auth::login_user")
        self.assertEqual(body["session"]["timeline"][-1]["payload"]["indexed_symbols_used"], ["auth::login_user"])

    def test_websocket_rejects_untrusted_origin(self) -> None:
        created = self.client.post("/api/incidents/", json=self.payload).json()

        with self.client.websocket_connect(
            f"/ws/incident/{created['session_id']}",
            headers={"origin": "https://evil.example"},
        ) as websocket:
            event = websocket.receive_json()

        self.assertEqual(event["type"], "error")
        self.assertEqual(event["detail"], "WebSocket origin is not allowed.")

    def test_model_client_path_builder_supports_base_and_v1_urls(self) -> None:
        self.assertEqual(_chat_completions_path("https://your-litellm-endpoint.example.com"), ("https://your-litellm-endpoint.example.com", "/chat/completions"))
        self.assertEqual(_chat_completions_path("https://your-litellm-endpoint.example.com/v1"), ("https://your-litellm-endpoint.example.com/v1", "/chat/completions"))

    def test_model_client_extracts_json_from_markdown_block(self) -> None:
        payload = _extract_json_payload(
            "```json\n{\"root_cause\": \"x\", \"patch\": \"y\", \"confidence\": []}\n```"
        )

        self.assertEqual(payload["root_cause"], "x")

    @patch("app.main.hosted_model_client.test_connection")
    def test_model_status_returns_connection_state(self, test_connection) -> None:
        test_connection.return_value = True

        response = self.client.get("/api/model/status")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["connected"])

    def test_chat_message_appends_to_timeline_without_state_change(self) -> None:
        created = self.client.post(
            "/api/incidents/",
            json={
                "fingerprint": {
                    "service": "chat-service",
                    "panic_type": "panic",
                    "top_frame": "src/main.rs:50",
                    "commit_hash": "chat123",
                },
                "environment": "UAT",
                "build_id": "build-chat",
                "raw_log": "panic in chat handler",
                "source": "manual",
            },
        ).json()

        response = self.client.post(
            f"/api/incidents/{created['session_id']}/chat",
            json={"role": "user", "content": "Why did this panic occur?"},
        ).json()

        self.assertEqual(response["state"], "CREATED")
        self.assertEqual(len(response["timeline"]), 2)
        self.assertEqual(response["timeline"][1]["event_type"], "chat_message")
        self.assertEqual(response["timeline"][1]["payload"]["role"], "user")
        self.assertEqual(response["timeline"][1]["payload"]["content"], "Why did this panic occur?")

    @patch("app.core.incident_service.run_incident_workflow")
    def test_analysis_complete_logged_to_timeline(self, run_incident_workflow) -> None:
        run_incident_workflow.return_value = IncidentAnalysis(
            root_cause="Timeline test root cause",
            patch="// Timeline test patch",
            confidence=[ConfidenceScore(label="Test", value="0.95")],
            context={"primary_symbol": "timeline::test"},
        )
        created = self.client.post(
            "/api/incidents/",
            json={
                "fingerprint": {
                    "service": "timeline-service",
                    "panic_type": "panic",
                    "top_frame": "src/timeline.rs:100",
                    "commit_hash": "timeline123",
                },
                "environment": "UAT",
                "build_id": "build-timeline",
                "raw_log": "timeline panic",
                "source": "manual",
            },
        ).json()

        self.client.post(f"/api/incidents/{created['session_id']}/analyze", json={})
        session = self.client.get(f"/api/incidents/{created['session_id']}").json()

        analysis_event = next((e for e in session["timeline"] if e["event_type"] == "analysis_complete"), None)
        self.assertIsNotNone(analysis_event)
        self.assertEqual(analysis_event["payload"]["root_cause"], "Timeline test root cause")


if __name__ == "__main__":
    unittest.main()