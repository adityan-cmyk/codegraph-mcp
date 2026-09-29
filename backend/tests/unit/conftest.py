"""Test isolation guard — MUST run before app modules are imported.

If tests run inside a container (or shell) that has real backend env vars set
(e.g. `docker compose run` leaks the compose env: SEMANTIC_INDEX_BACKEND,
GRAPH_INDEX_BACKEND, INDEX_METADATA_BACKEND, POSTGRES_*, NEO4J_*, WEAVIATE_*),
the app singletons bind to the LIVE backends. reset() calls in setUp() then
wipe the production graph, semantic index, and build registry — a total index
outage (this happened once; recovery required a full re-index).

Three layers of protection:
  1. This conftest forces all backend selectors to in-memory fakes.
  2. Real-backend reset() methods refuse to run under pytest (app/core/test_guard.py).
  3. scripts/run-tests.sh runs pytest in a --network none container — no
     network path to any real backend even if layers 1-2 both fail.

To run tests against real backends (not recommended), set
CODEGRAPH_TESTS_ALLOW_REAL_BACKENDS=1 explicitly.
"""

import os

if not os.environ.get("CODEGRAPH_TESTS_ALLOW_REAL_BACKENDS"):
    # Force in-memory backend selectors (the .env / compose values select real backends)
    os.environ["SEMANTIC_INDEX_BACKEND"] = "memory"
    os.environ["GRAPH_INDEX_BACKEND"] = "memory"
    os.environ["INDEX_METADATA_BACKEND"] = "memory"

    # Strip any real backend connection vars
    for var in (
        "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
        "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
        "WEAVIATE_URL", "WEAVIATE_API_KEY", "WEAVIATE_GRPC_ENABLED",
        "REDIS_URL",
        "T2V_INFERENCE_URL",
    ):
        os.environ.pop(var, None)
