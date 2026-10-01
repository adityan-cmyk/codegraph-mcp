"""Prometheus metrics for the codegraph-mcp backend.

All latency metrics are recorded in MILLISECONDS and exported in Prometheus
exposition format at GET /api/metrics (bearer-token protected like all /api
routes; Prometheus sends the token from its scrape config).

Covers: API request latency, MCP tool calls + latency, semantic search,
build progress/duration, feedback gate (decision model) latency + verdicts,
feedback submissions, emails, index freshness, and graph size.
"""

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Buckets tuned for the observed ranges: MCP tools 5ms-30s, gate 5-40s,
# API endpoints 1-500ms.
_MS_BUCKETS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
_SLOW_MS_BUCKETS = (10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000)

API_REQUEST_LATENCY_MS = Histogram(
    "api_request_latency_ms",
    "REST API request latency",
    ["method", "path"],
    buckets=_MS_BUCKETS,
)

MCP_TOOL_CALLS = Counter(
    "mcp_tool_calls_total",
    "MCP tool invocations",
    ["tool", "status"],
)

MCP_TOOL_LATENCY_MS = Histogram(
    "mcp_tool_latency_ms",
    "MCP tool end-to-end latency",
    ["tool"],
    buckets=_SLOW_MS_BUCKETS,
)

SEMANTIC_SEARCH_LATENCY_MS = Histogram(
    "semantic_search_latency_ms",
    "semantic_search internal latency (hybrid + rerank)",
    buckets=_SLOW_MS_BUCKETS,
)

BUILD_BATCHES = Counter(
    "build_batches_total",
    "Embedding batches inserted (64 chunks each)",
)

BUILD_DURATION_MS = Histogram(
    "build_duration_ms",
    "Full build duration",
    ["build_type"],
    buckets=(60_000, 300_000, 600_000, 1_800_000, 3_600_000, 7_200_000, 10_800_000),
)

GATE_LATENCY_MS = Histogram(
    "gate_latency_ms",
    "Decision-model (systemone) feedback gate latency",
    buckets=(1_000, 5_000, 10_000, 20_000, 30_000, 45_000, 60_000, 90_000),
)

GATE_VERDICTS = Counter(
    "gate_verdicts_total",
    "Feedback gate verdicts",
    ["verdict"],
)

FEEDBACK_SUBMISSIONS = Counter(
    "feedback_submissions_total",
    "AI feedback submissions received",
)

FEEDBACK_CLASSIFICATIONS = Counter(
    "feedback_classifications_total",
    "Feedback routing classification (what the feedback is about)",
    ["type"],
)

EMAILS = Counter(
    "emails_total",
    "Emails sent via SMTP",
    ["status"],
)

INDEX_STALE_HOURS = Gauge(
    "index_stale_hours",
    "Hours the index is behind repo HEAD (0 = fresh)",
)

GRAPH_NODES = Gauge("graph_nodes_total", "Symbols in the active graph generation")
GRAPH_EDGES = Gauge("graph_edges_total", "Edges in the active graph generation")


def exposition() -> tuple[bytes, str]:
    """Return (body, content_type) for the /api/metrics endpoint."""
    return generate_latest(), CONTENT_TYPE_LATEST
