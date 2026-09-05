import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routers.feedback import router as feedback_router
from app.api.routers.graph import router as graph_router
from app.api.routers.indexing import router as indexing_router
from app.core.config import settings
from app.core.health import check_all_backends, check_readiness
from app.core.metrics import metrics_collector
from app.core.tracing_service import tracing_service
from app.rag.indexing_service import index_rust_repository, replay_indexes_from_storage

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import subprocess
    try:
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", settings.codebase_root_path or ""],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except Exception:
        pass

    if settings.index_on_startup and settings.codebase_root_path:
        logger.info("Auto-indexing repository on startup: %s", settings.codebase_root_path)
        import asyncio
        asyncio.get_event_loop().run_in_executor(None, index_rust_repository, None)
    elif settings.index_replay_on_startup:
        replay_indexes_from_storage()

    import threading
    import uvicorn
    from app.mcp.readonly_server import readonly_mcp_server

    def _run_readonly_mcp():
        config = uvicorn.Config(
            app=readonly_mcp_server,
            host=settings.readonly_mcp_host,
            port=settings.readonly_mcp_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        server.run()

    mcp_thread = threading.Thread(target=_run_readonly_mcp, daemon=True, name="readonly-mcp")
    mcp_thread.start()
    logger.info(
        "Read-only MCP server (stateless) started on %s:%d/mcp",
        settings.readonly_mcp_host,
        settings.readonly_mcp_port,
    )

    try:
        from app.rag.reinforcement.agent import start_agent as start_reinforcement_agent
        start_reinforcement_agent()
    except Exception:
        logger.warning("Failed to start reinforcement agent", exc_info=True)

    yield


app = FastAPI(title="Codegraph MCP API", lifespan=lifespan)

from app.core.auth import AuthMiddleware
from app.core.rate_limit import RateLimitMiddleware
from app.core.structured_logging import TraceIdMiddleware, setup_structured_logging

setup_structured_logging()

app.add_middleware(RateLimitMiddleware)
app.add_middleware(TraceIdMiddleware)
app.add_middleware(AuthMiddleware)

from fastapi.middleware.trustedhost import TrustedHostMiddleware
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Accept", "Authorization", "Content-Type"],
)

app.include_router(indexing_router)
app.include_router(graph_router)
app.include_router(feedback_router)


@app.get("/health")
def healthcheck() -> dict[str, object]:
    return {"status": "ok"}


@app.get("/ready")
def readiness_check() -> dict[str, object]:
    return check_readiness()


@app.get("/api/health")
def detailed_health() -> dict[str, object]:
    return check_all_backends()


@app.get("/api/metrics")
def get_metrics() -> dict[str, object]:
    return metrics_collector.snapshot()


@app.get("/api/tracing/status")
def tracing_status() -> dict[str, object]:
    return {"enabled": tracing_service.is_enabled, "backend": "langfuse" if tracing_service.is_enabled else None}
