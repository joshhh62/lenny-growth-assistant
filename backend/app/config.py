"""Application configuration.

Everything an operator can change lives here and is read from environment
variables (or a `.env` file). No application code needs to change to switch
LLM provider, model, database or retrieval behaviour — see `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["anthropic", "ollama"]
Runtime = Literal["agent_sdk", "messages_loop", "auto"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- App -----------------------------------------------------------------
    app_name: str = "Lenny Growth Assistant"
    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # --- Database ------------------------------------------------------------
    # Postgres. Local docker-compose default; Supabase/Railway URLs work as-is
    # (use the *asyncpg* driver: postgresql+asyncpg://...).
    database_url: str = "postgresql+asyncpg://lenny:lenny@localhost:5432/lenny"
    db_pool_size: int = 5
    db_connect_timeout_s: float = 5.0

    # --- LLM provider toggle -------------------------------------------------
    # Which provider serves chat by default. The UI can also pick per session.
    llm_provider: Provider = "ollama"
    # If the chosen provider is unhealthy (no key, Ollama down, timeout),
    # fall back to this one when it is available. Empty string disables.
    llm_fallback_provider: str = "anthropic"
    # Which agent runtime to use. `auto` = agent_sdk for anthropic, messages_loop
    # for ollama (see architecture.md → "Agent runtimes").
    agent_runtime: Runtime = "auto"
    llm_timeout_s: float = 120.0
    llm_max_tool_rounds: int = 6
    llm_max_output_tokens: int = 2048

    # Anthropic (cloud)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # Ollama (local)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_num_ctx: int = 8192

    # --- Retrieval -----------------------------------------------------------
    transcripts_dir: str = "./data/transcripts"
    transcripts_repo_url: str = "https://github.com/ChatPRD/lennys-podcast-transcripts.git"
    # Limit the number of episodes ingested (0 = all). Useful for quick evals.
    ingest_episode_limit: int = 0
    chunk_target_tokens: int = 350
    chunk_overlap_turns: int = 1
    retrieval_top_k: int = 6
    # Vector hits below this cosine similarity are discarded, so off-topic
    # questions produce an honest "no passages" instead of nearest-neighbour noise.
    # (nomic-embed-text: related ≈ 0.6+, unrelated ≈ 0.3–0.45.)
    vector_min_similarity: float = 0.45
    # Compute embeddings in the background after ingest (requires Ollama).
    embeddings_enabled: bool = True
    embed_batch_size: int = 32
    embedding_dim: int = 768  # nomic-embed-text

    # --- Artifacts -----------------------------------------------------------
    artifact_max_bytes: int = 200_000

    @field_validator("llm_fallback_provider")
    @classmethod
    def _validate_fallback(cls, v: str) -> str:
        if v and v not in ("anthropic", "ollama"):
            raise ValueError("llm_fallback_provider must be 'anthropic', 'ollama' or empty")
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def model_for(self, provider: Provider) -> str:
        return self.anthropic_model if provider == "anthropic" else self.ollama_model

    def runtime_for(self, provider: Provider) -> Runtime:
        if self.agent_runtime != "auto":
            return self.agent_runtime
        return "agent_sdk" if provider == "anthropic" else "messages_loop"


@lru_cache
def get_settings() -> Settings:
    return Settings()
