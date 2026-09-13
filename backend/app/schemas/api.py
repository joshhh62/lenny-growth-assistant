"""Request/response contracts. Pydantic v2 — validated at the edge."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- sessions ---------------------------------------------------------------
class SessionCreate(BaseModel):
    user_id: str | None = Field(None, max_length=128, description="Client-generated anonymous id")
    display_name: str | None = Field(None, max_length=120)
    provider: Literal["anthropic", "ollama"] | None = Field(None, description="Defaults to LLM_PROVIDER")
    title: str | None = Field(None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionUpdate(BaseModel):
    title: str | None = Field(None, max_length=200)
    provider: Literal["anthropic", "ollama"] | None = None


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: str | None
    title: str
    provider: str
    model: str
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    message_count: int | None = None


class MessageOut(BaseModel):
    id: UUID
    session_id: UUID
    role: str
    content: str
    citations: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    provider: str | None
    model: str | None
    runtime: str | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    created_at: datetime


class ArtifactSummary(BaseModel):
    id: UUID
    session_id: UUID
    message_id: UUID | None
    kind: str
    title: str
    created_at: datetime


class ArtifactOut(ArtifactSummary):
    content: str
    # From the assistant message that produced it, so the viewer can turn the
    # artifact's inline [n] markers into working timestamp links.
    citations: list[dict[str, Any]] = []


class SessionDetail(BaseModel):
    session: SessionOut
    messages: list[MessageOut]
    artifacts: list[ArtifactSummary]


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=8000)


# --- config / health --------------------------------------------------------
class ProviderInfo(BaseModel):
    name: str
    available: bool
    model: str
    reason: str = ""
    latency_ms: float | None = None
    runtime: str = ""
    warning: str = ""


class ConfigOut(BaseModel):
    app_name: str
    default_provider: str
    fallback_provider: str | None
    providers: list[ProviderInfo]
    retrieval: dict[str, Any]


class SearchHit(BaseModel):
    n: int
    episode_id: str
    guest: str
    title: str
    speaker: str | None
    start_seconds: int | None
    timestamp_url: str | None
    score: float
    sources: list[str]
    text: str


class SearchOut(BaseModel):
    query: str
    mode: str
    duration_ms: float
    hits: list[SearchHit]
