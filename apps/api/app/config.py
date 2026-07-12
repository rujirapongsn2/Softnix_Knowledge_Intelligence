from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "Softnix Knowledge Intelligence Platform"
    app_env: str = "development"
    app_secret_key: str = "development-only-change-me"
    database_url: str = "sqlite:///./data/skip.db"
    redis_url: str = ""
    file_storage_path: str = "./data/files"
    max_file_size_mb: int = 100
    token_prefix: str = "skik_live_"
    token_hash_secret: str = "development-only-token-secret"
    initial_admin_username: str = "admin"
    initial_admin_password: str = "change-me-now"
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    cookie_secure: bool = False
    default_chunk_size: int = 800
    default_chunk_overlap: int = 120
    default_top_k: int = 12
    max_graph_depth: int = 3
    log_query_text: bool = True
    lightrag_base_url: str = ""
    lightrag_api_key: str = ""
    lightrag_timeout_seconds: int = 120
    lightrag_processing_timeout_seconds: int = 300
    lightrag_workspace_prefix: str = "skip-"
    neo4j_http_url: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_timeout_seconds: int = 5
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key: str = ""
    openrouter_llm_model: str = "openai/gpt-4o-mini"
    openrouter_embedding_model: str = "openai/text-embedding-3-small"
    openrouter_embedding_dimension: int = 1536
    openrouter_rerank_model: str = ""
    rerank_candidate_limit: int = 20
    openrouter_app_url: str = ""
    openrouter_app_title: str = "Softnix Knowledge Intelligence Platform"
    reranker_enabled: bool = False
    ext_ocr_base_url: str = "https://111.223.37.41:9001"
    ext_ocr_key: str = ""
    ext_ocr_verify_ssl: bool = False
    ext_ocr_engine: str = "tesseract"
    ext_ocr_image_size: int = 1800
    ext_ocr_request_timeout_seconds: int = 60
    ext_ocr_processing_timeout_seconds: int = 300
    ext_ocr_poll_interval_seconds: int = 2

    @property
    def file_root(self) -> Path:
        return Path(self.file_storage_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
