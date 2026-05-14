from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "On-call Assistant"
    environment: str = "local"
    redis_url: str = "redis://localhost:6379/0"
    postgres_dsn: str = "postgresql://localhost/oncall"
    weaviate_url: str = "http://localhost:8080"
    neo4j_url: str = "bolt://localhost:7687"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()