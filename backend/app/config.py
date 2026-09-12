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
    # Read timeout per model request. Local CPU models can take minutes to process
    # a grounded prompt before the first token, so this is generous by default.
    llm_timeout_s: float = 300.0
    llm_max_tool_rounds: int = 6
    llm_max_output_tokens: int = 2048

    # Anthropic (cloud)
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5"

    # Ollama (local)
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_embed_model: str = "nomic-embed-text"
    # CPU-bound models re-process the whole prompt on every tool round, so by
    # default local chat relies on retrieve-then-generate (passages are always
    # injected) and does not expose tools to the model. Skills (essay, artifact)
    # are routed deterministically and unaffected. Flip on for a stronger local box.
    ollama_tools_enabled: bool = False
    # Shorter answers locally: at ~8 tok/s, 800 tokens is already ~100 s worst case.
    ollama_max_output_tokens: int = 800
    # Minimum context window we expect Ollama to run the model with. Readiness
    # warns when the loaded model reports less (prompts would be truncated).
    ollama_min_context: int = 8192
    # Fewer passages for the local model: prompt size is the main driver of
    # time-to-first-token on CPU (each passage ≈ 350 tokens).
    ollama_retrieval_top_k: int = 4

    # --- Retrieval -----------------------------------------------------------
    transcripts_dir: str = "./data/transcripts"
    transcripts_repo_url: str = "https://github.com/ChatPRD/lennys-podcast-transcripts.git"
    # Limit the number of episodes ingested (0 = all). Useful for quick evals.
    ingest_episode_limit: int = 0
    # On startup, if the knowledge base is empty, clone + ingest automatically.
    auto_ingest_on_start: bool = True
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

    def tools_enabled_for(self, provider: Provider) -> bool:
        return True if provider == "anthropic" else self.ollama_tools_enabled

    def retrieval_top_k_for(self, provider: Provider) -> int:
        return self.retrieval_top_k if provider == "anthropic" else self.ollama_retrieval_top_k

    def max_output_tokens_for(self, provider: Provider) -> int:
        return self.llm_max_output_tokens if provider == "anthropic" else self.ollama_max_output_tokens

    def runtime_for(self, provider: Provider) -> Runtime:
        if self.agent_runtime != "auto":
            return self.agent_runtime
        return "agent_sdk" if provider == "anthropic" else "messages_loop"


@lru_cache
def get_settings() -> Settings:
    return Settings()
