from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


LEGACY_FIELDS = ("llm_base_url", "llm_api_key", "llm_model", "llm_reasoning_effort", "rerank_model", "llm_app_url", "llm_app_title")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)
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
    lightrag_processing_timeout_seconds: int = 900
    # Queue health: a running job refreshes updated_at every job_heartbeat_seconds; one not refreshed for
    # job_stale_after_seconds lost its worker and is re-queued by the stale-job sweep.
    job_heartbeat_seconds: int = 30
    job_stale_after_seconds: int = 900
    # How long an ingest call above its token's rate or concurrency waits for a slot before answering 429.
    # Longer than the 60 s rate window, so a burst always reaches the next window; shorter than the proxy timeouts.
    ingest_limit_wait_seconds: int = 65
    lightrag_workspace_prefix: str = "skip-"
    # The model LightRAG extracts the graph with (compose passes it to LightRAG); empty means llm_model.
    lightrag_llm_model: str = ""
    neo4j_http_url: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    neo4j_database: str = "neo4j"
    neo4j_timeout_seconds: int = 5
    # Any OpenAI-compatible chat completions server; the legacy_* fields below read the names of earlier releases.
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_api_key: str = ""
    llm_model: str = "anthropic/claude-sonnet-5.5"
    # openrouter, openai or compatible (see llm.DIALECTS); empty picks one from llm_base_url.
    llm_api_dialect: Literal["", "openrouter", "openai", "compatible"] = ""
    # Empty sends no temperature, for models that accept only their default (OpenAI reasoning models).
    llm_temperature: float | None = 0
    # Reasoning tokens count against the output budget, and Sonnet 5.5 at its default effort spent 5.7k of a 6k
    # budget on one extraction window and returned cut-off JSON. Empty sends nothing.
    llm_reasoning_effort: str = "low"
    # Any OpenAI-compatible POST {base}/embeddings server; empty disables embeddings.
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "embeddinggemma-2"
    # Also sizes the pgvector column; see migration 0039 for changing it on an existing database.
    embedding_dimension: int = 768
    # embeddinggemma's task prompts; the query and document sides must use matching ones.
    embedding_query_prefix: str = "task: search result | query: "
    embedding_document_prefix: str = "title: none | text: "
    # POST {base}/rerank on a server speaking rerank_api_style (see llm.RERANK_STYLES). An empty base URL means the
    # LLM's server; an empty key means the LLM's key only when the rerank server is the LLM's, else no key.
    rerank_base_url: str = ""
    rerank_api_key: str = ""
    rerank_api_style: Literal["cohere", "tei"] = "cohere"
    # False accepts a self-signed certificate on a rerank server inside a trusted network.
    rerank_verify_ssl: bool = True
    rerank_model: str = ""
    rerank_candidate_limit: int = 20
    # Sent as OpenRouter's app attribution headers; llm_app_url is also the fallback for public_app_url.
    llm_app_url: str = ""
    # Customer-facing origin for absolute citation/download links in MCP answers.
    public_app_url: str = ""
    llm_app_title: str = "Softnix Knowledge Intelligence Platform"
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

    legacy_llm_base_url: str = Field("", validation_alias="OPENROUTER_BASE_URL", exclude=True)
    legacy_llm_api_key: str = Field("", validation_alias="OPENROUTER_API_KEY", exclude=True)
    legacy_llm_model: str = Field("", validation_alias="OPENROUTER_LLM_MODEL", exclude=True)
    legacy_llm_reasoning_effort: str = Field("", validation_alias="OPENROUTER_REASONING_EFFORT", exclude=True)
    legacy_rerank_model: str = Field("", validation_alias="OPENROUTER_RERANK_MODEL", exclude=True)
    legacy_llm_app_url: str = Field("", validation_alias="OPENROUTER_APP_URL", exclude=True)
    legacy_llm_app_title: str = Field("", validation_alias="OPENROUTER_APP_TITLE", exclude=True)

    @model_validator(mode="after")
    def _fall_back_to_legacy_names(self):
        # Like docker-compose's ${LLM_API_KEY:-${OPENROUTER_API_KEY}}: an LLM_* name that is unset or empty takes
        # the OPENROUTER_* value of earlier releases, so the api and LightRAG never end up with different keys.
        for field in LEGACY_FIELDS:
            old = getattr(self, f"legacy_{field}")
            if old and (field not in self.model_fields_set or not getattr(self, field)):
                setattr(self, field, old)
        return self

    @field_validator("llm_temperature", mode="before")
    @classmethod
    def _empty_temperature_is_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def file_root(self) -> Path:
        return Path(self.file_storage_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
