"""Guard against tests wiping real backends.

pytest sets PYTEST_CURRENT_TEST during each test. If that variable is set,
reset() methods on REAL backend implementations (Postgres, Neo4j, Weaviate)
must refuse to run — a test setUp() calling reset() against live infrastructure
deletes production index data.

Tests should run against in-memory fakes (see tests/unit/conftest.py, which
strips backend env vars). If you genuinely need to reset a real backend, do it
via the admin API or psql/cypher-shell directly — never from a test.
"""

import os


def refuse_reset_under_pytest(backend: str) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError(
            f"Refusing to reset real {backend} while running under pytest — "
            "this would wipe live index data. Run tests via scripts/run-tests.sh "
            "(network-isolated container). If you intentionally set "
            "CODEGRAPH_TESTS_ALLOW_REAL_BACKENDS=1, unset it."
        )
