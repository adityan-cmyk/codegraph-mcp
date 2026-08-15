import os
import unittest
from unittest.mock import patch, MagicMock

from app.core.tracing_service import TracingService, _NoOpSpan


class TracingServiceNoOpTestCase(unittest.TestCase):
    def test_disabled_service_yields_noop_span(self) -> None:
        service = TracingService.__new__(TracingService)
        service._client = None
        service._enabled = False

        self.assertFalse(service.is_enabled)

        with service.trace("test_trace") as span:
            self.assertIsInstance(span, _NoOpSpan)
            span.update(key="value")
            span.add_event(name="test")

        with service.span("test_span") as span:
            self.assertIsInstance(span, _NoOpSpan)

        with service.generation("test_gen") as span:
            self.assertIsInstance(span, _NoOpSpan)

    def test_update_generation_on_noop_does_nothing(self) -> None:
        service = TracingService.__new__(TracingService)
        service._client = None
        service._enabled = False

        noop = _NoOpSpan()
        service.update_generation(noop, output={"test": True}, usage={"tokens": 10})


class TracingServiceEnabledTestCase(unittest.TestCase):
    @patch("app.core.tracing_service.TracingService.__init__", return_value=None)
    def test_enabled_service_creates_trace(self, mock_init) -> None:
        service = TracingService.__new__(TracingService)
        mock_client = MagicMock()
        service._client = mock_client
        service._enabled = True

        mock_trace = MagicMock()
        mock_client.trace.return_value = mock_trace

        with service.trace("test_trace", metadata={"key": "value"}) as trace:
            self.assertEqual(trace, mock_trace)

        mock_client.trace.assert_called_once_with(name="test_trace", metadata={"key": "value"})
        mock_trace.update_end_time.assert_called_once()

    @patch("app.core.tracing_service.TracingService.__init__", return_value=None)
    def test_enabled_service_creates_span(self, mock_init) -> None:
        service = TracingService.__new__(TracingService)
        mock_client = MagicMock()
        service._client = mock_client
        service._enabled = True

        mock_span = MagicMock()
        mock_client.span.return_value = mock_span

        with service.span("test_span", trace_id="abc123") as span:
            self.assertEqual(span, mock_span)

        mock_client.span.assert_called_once_with(name="test_span", trace_id="abc123", metadata=None)
        mock_span.update_end_time.assert_called_once()

    @patch("app.core.tracing_service.TracingService.__init__", return_value=None)
    def test_enabled_service_creates_generation(self, mock_init) -> None:
        service = TracingService.__new__(TracingService)
        mock_client = MagicMock()
        service._client = mock_client
        service._enabled = True

        mock_gen = MagicMock()
        mock_client.generation.return_value = mock_gen

        with service.generation("test_gen", model="test-model", input={"msg": "hi"}) as gen:
            self.assertEqual(gen, mock_gen)

        mock_client.generation.assert_called_once_with(
            name="test_gen", trace_id=None, model="test-model", input={"msg": "hi"}
        )
        mock_gen.update_end_time.assert_called_once()

    @patch("app.core.tracing_service.TracingService.__init__", return_value=None)
    def test_update_generation_with_output_and_usage(self, mock_init) -> None:
        service = TracingService.__new__(TracingService)
        mock_gen = MagicMock()
        service.update_generation(mock_gen, output={"text": "hello"}, usage={"tokens": 5})

        mock_gen.update.assert_called_once_with(output={"text": "hello"}, usage={"tokens": 5})


@unittest.skipUnless(
    os.environ.get("TEST_LANGFUSE_ENABLED"),
    "Set TEST_LANGFUSE_ENABLED=1 with LANGFUSE_PUBLIC_KEY/SECRET_KEY to run Langfuse integration tests",
)
class LangfuseIntegrationTestCase(unittest.TestCase):
    def test_tracing_status_endpoint_reports_enabled(self) -> None:
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        response = client.get("/api/tracing/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["backend"], "langfuse")

    def test_trace_emits_to_langfuse(self) -> None:
        from app.core.config import settings

        service = TracingService.__new__(TracingService)
        service._enabled = True

        from langfuse import Langfuse
        service._client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )

        with service.trace("integration_test_trace") as trace:
            with service.span("test_span", trace_id=trace.id if hasattr(trace, 'id') else None) as span:
                with service.generation("test_gen", model="test", input={"msg": "test"}) as gen:
                    service.update_generation(gen, output={"reply": "hello"}, usage={"tokens": 2})

        service._client.flush()


if __name__ == "__main__":
    unittest.main()
