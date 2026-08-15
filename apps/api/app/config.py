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
    observability_retention_days: int = 30
    audit_retention_days: int = 180
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
    retrieval_planner_timeout_seconds: int = 4
    # --- OCR chain (anydoc pipeline) -------------------------------------
    # SOFTNIX_OCR_* / MISTRAL_API_KEY / OCR_CHAIN_ENGINES configure the
    # Softnix → Mistral → Tesseract fallback chain in app/ocr_chain.py.
    softnix_ocr_base_url: str = ""
    softnix_ocr_token: str = ""
    softnix_ocr_insecure_tls: bool = False
    # Per-page budget for the whole Softnix job (submit + poll + result).
    # Kept short because the chain must fall through to Mistral quickly when
    # the Softnix queue is degraded; a slow queue must not stall a document.
    softnix_ocr_timeout_seconds: int = 120
    # HTTP timeout for each individual request (submit/status/result).
    softnix_ocr_request_timeout_seconds: int = 30
    # Abort when the job reports no state/progress change for this long —
    # the known failure mode is a job stuck in "queueing" that never
    # reports progress and would otherwise burn the whole budget.
    softnix_ocr_stall_seconds: float = 45.0
    softnix_ocr_poll_interval_seconds: float = 2.0
    mistral_api_key: str = ""
    mistral_ocr_timeout_seconds: int = 60
    tesseract_timeout_seconds: int = 120
    ocr_chain_engines: str = "softnix,mistral,tesseract"

    @property
    def file_root(self) -> Path:
        return Path(self.file_storage_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
