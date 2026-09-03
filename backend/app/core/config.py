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
    app_name: str = "On-call Assistant"
    environment: str = "local"
    incident_store_backend: str = "memory"
    index_metadata_backend: str = "memory"
    resolved_error_backend: str = "memory"
    eval_case_backend: str = "memory"
    semantic_index_backend: str = "memory"
    graph_index_backend: str = "memory"
    index_replay_on_startup: bool = True
    index_on_startup: bool = False
    codebase_root_path: str | None = None
    indexing_allowed_roots: list[str] = Field(default_factory=lambda: [str(ROOT_DIR)])
    redis_url: str = "redis://localhost:6379/0"
    celery_task_always_eager: bool = False
    weaviate_url: str = "http://localhost:8080"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_username: str = "neo4j"
    neo4j_password: str = "password"
    postgres_dsn: str = "postgresql://localhost/oncall"
    litellm_api_key: str | None = None
    litellm_base_url: str | None = None
    litellm_model: str = "glm-latest"
    litellm_embedding_model: str = "gemini-embedding-001"
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

    # Tracing
    langfuse_enabled: bool = False
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Auth
    api_auth_token: str | None = None
    mcp_auth_token: str | None = None

    # SMTP notifications
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
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