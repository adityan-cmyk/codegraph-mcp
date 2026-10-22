from pathlib import Path
from typing import Literal
import json

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT_DIR = Path(__file__).resolve().parents[3]


def _split_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    stripped = value.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    app_name: str = "Codegraph MCP"
    environment: str = "local"
    index_metadata_backend: str = "memory"
    semantic_index_backend: str = "memory"
    graph_index_backend: str = "memory"
    index_replay_on_startup: bool = True
    index_on_startup: bool = False
    codebase_root_path: str | None = None
    indexing_allowed_roots: list[str] = Field(default_factory=lambda: [str(ROOT_DIR)])
    redis_url: str = "redis://localhost:6379/0"
    weaviate_url: str = "http://localhost:8080"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    postgres_dsn: str = "postgresql://localhost/oncall"
    readonly_mcp_transport: Literal["stdio", "http", "streamable-http", "sse"] = "streamable-http"
    readonly_mcp_host: str = "0.0.0.0"
    readonly_mcp_port: int = 8002
    readonly_mcp_path: str = "/mcp"
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:4173",
            "http://127.0.0.1:4173",
        ]
    )
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "testserver"])
    # Only these socket peers may assert X-Forwarded-For (Tailscale Funnel
    # arrives via the Docker bridge gateway). Direct LAN clients are outside
    # these CIDRs — their XFF headers are ignored, so a LAN user cannot spoof
    # past the brute-force lockout or per-IP token buckets.
    trusted_proxy_cidrs: list[str] = Field(
        default_factory=lambda: ["127.0.0.0/8", "::1/128", "172.16.0.0/12"]
    )
    # Include tracebacks (paths, query text) in MCP tool-error payloads.
    tool_error_tracebacks: bool = True

    # Tracing
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Auth
    api_auth_token: str | None = None
    mcp_auth_token: str | None = None
    admin_panel_token: str | None = None
    # Per-user key store: "memory" (tests/local) or "postgres" (real deploys).
    client_tokens_backend: str = "memory"
    reranker_enabled: bool = False
    reranker_url: str = "http://reranker:8001"
    # SCIP refinement: replace regex edges with rust-analyzer-resolved ones
    # for files the index covers (see scripts/generate-scip.sh)
    scip_refine_enabled: bool = False
    scip_index_path: str = ""

    # SMTP notifications
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None

    # Local decision model (Ollama /v1/systemone) for feedback quality gating.
    # Empty = disabled, heuristic gate is used.
    systemone_url: str | None = None
    systemone_model: str = "nimble"
    systemone_timeout: int = 90
    smtp_to: list[str] = Field(default_factory=list)
    smtp_use_tls: bool = True

    # Outbound integration timeouts
    model_connect_timeout: int = 10
    model_read_timeout: int = 120
    model_max_retries: int = 2
    weaviate_query_timeout: int = 10
    neo4j_query_timeout: int = 10
    postgres_query_timeout: int = 30

    model_config = SettingsConfigDict(env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("cors_allowed_origins", "trusted_hosts", mode="before")
    @classmethod
    def parse_list_env(cls, value: str | list[str] | None) -> list[str]:
        return _split_csv(value)


settings = Settings()