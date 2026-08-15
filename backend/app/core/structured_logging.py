"""Structured logging with trace IDs.

Adds request ID middleware that generates a unique trace ID per request,
injects it into the logging context, and returns it in response headers.
All log entries include the trace ID for correlation across services.
"""

import logging
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


class TraceIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
        _trace_id.set(trace_id)

        response: Response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response


class TraceIdFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = _trace_id.get("-")
        return True


def setup_structured_logging():
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","trace_id":"%(trace_id)s","logger":"%(name)s","msg":"%(message)s"}',
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.addFilter(TraceIdFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def get_trace_id() -> str:
    return _trace_id.get("-")
