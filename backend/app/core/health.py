from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings


@dataclass
class BackendHealthResult:
    backend: str
    status: str
    detail: str = ""
    latency_ms: float = 0.0


def check_postgres() -> BackendHealthResult:
    if settings.incident_store_backend != "postgres":
        return BackendHealthResult(backend="postgres", status="disabled", detail="Not configured as active backend")
    try:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(settings.postgres_dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 AS ok")
                row = cur.fetchone()
                if row and row.get("ok") == 1:
                    return BackendHealthResult(backend="postgres", status="healthy")
        return BackendHealthResult(backend="postgres", status="degraded", detail="Query returned unexpected result")
    except Exception as exc:
        return BackendHealthResult(backend="postgres", status="unhealthy", detail=str(exc))


def check_redis() -> BackendHealthResult:
    try:
        import redis as redis_lib

        client = redis_lib.from_url(settings.redis_url, socket_connect_timeout=3)
        pong = client.ping()
        client.close()
        if pong:
            return BackendHealthResult(backend="redis", status="healthy")
        return BackendHealthResult(backend="redis", status="unhealthy", detail="PING returned False")
    except Exception as exc:
        return BackendHealthResult(backend="redis", status="unhealthy", detail=str(exc))


def check_weaviate() -> BackendHealthResult:
    if settings.semantic_index_backend != "weaviate":
        return BackendHealthResult(backend="weaviate", status="disabled", detail="Not configured as active backend")
    try:
        import httpx

        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{settings.weaviate_url}/v1/.well-known/ready")
            if response.status_code == 200:
                return BackendHealthResult(backend="weaviate", status="healthy")
            return BackendHealthResult(
                backend="weaviate", status="unhealthy", detail=f"Ready endpoint returned {response.status_code}"
            )
    except Exception as exc:
        return BackendHealthResult(backend="weaviate", status="unhealthy", detail=str(exc))


def check_neo4j() -> BackendHealthResult:
    if settings.graph_index_backend != "neo4j":
        return BackendHealthResult(backend="neo4j", status="disabled", detail="Not configured as active backend")
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password))
        try:
            with driver.session() as session:
                result = session.run("RETURN 1 AS ok")
                record = result.single()
                if record and record["ok"] == 1:
                    return BackendHealthResult(backend="neo4j", status="healthy")
                return BackendHealthResult(backend="neo4j", status="degraded", detail="Query returned unexpected result")
        finally:
            driver.close()
    except Exception as exc:
        return BackendHealthResult(backend="neo4j", status="unhealthy", detail=str(exc))


def check_all_backends() -> dict[str, Any]:
    checks = [
        check_postgres(),
        check_redis(),
        check_weaviate(),
        check_neo4j(),
    ]

    results = []
    all_healthy = True
    for check in checks:
        results.append(
            {
                "backend": check.backend,
                "status": check.status,
                "detail": check.detail,
            }
        )
        if check.status == "unhealthy":
            all_healthy = False

    overall = "healthy" if all_healthy else "degraded"
    has_any_enabled = any(c.status != "disabled" for c in checks)
    if not has_any_enabled:
        overall = "healthy"

    return {
        "status": overall,
        "backends": results,
    }


def check_model() -> BackendHealthResult:
    try:
        import httpx
        from app.core.config import settings
        if not settings.litellm_base_url:
            return BackendHealthResult(backend="model", status="disabled", detail="No LITELLM_BASE_URL configured")
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(
                f"{settings.litellm_base_url}/models",
                headers={"Authorization": f"Bearer {settings.litellm_api_key}"} if settings.litellm_api_key else {},
            )
            if resp.status_code == 200:
                return BackendHealthResult(backend="model", status="healthy")
            return BackendHealthResult(backend="model", status="unhealthy", detail=f"Model API returned {resp.status_code}")
    except Exception as exc:
        return BackendHealthResult(backend="model", status="unhealthy", detail=str(exc))


def check_readiness() -> dict[str, Any]:
    checks = [
        check_postgres(),
        check_weaviate(),
        check_neo4j(),
        check_model(),
    ]

    results = []
    all_ready = True
    for check in checks:
        if check.status == "disabled":
            continue
        results.append({"backend": check.backend, "status": check.status})
        if check.status != "healthy":
            all_ready = False

    return {
        "ready": all_ready,
        "backends": results,
    }
