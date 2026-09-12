"""LLM provider registry and fallback.

Both providers speak the Anthropic Messages API:
  * anthropic → api.anthropic.com with ANTHROPIC_API_KEY
  * ollama    → <OLLAMA_BASE_URL>/v1/messages (Ollama's Anthropic-compatible endpoint)

So one client library, one tool-calling format, and switching is pure config.
`resolve()` implements the documented fallback: if the requested provider is
unhealthy and a fallback is configured and healthy, use it and *tell the UI*.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

import anthropic
import httpx

from app.config import Provider, get_settings
from app.logging_setup import get_logger

log = get_logger("llm")


@dataclass
class ProviderStatus:
    name: str
    available: bool
    model: str
    reason: str = ""
    latency_ms: float | None = None
    runtime: str = ""
    warning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class NoProviderAvailable(RuntimeError):
    def __init__(self, statuses: list[ProviderStatus]):
        self.statuses = statuses
        super().__init__("; ".join(f"{s.name}: {s.reason}" for s in statuses))


async def check_ollama() -> ProviderStatus:
    s = get_settings()
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as c:
            r = await c.get(f"{s.ollama_base_url.rstrip('/')}/api/tags")
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
        wanted = s.ollama_model if ":" in s.ollama_model else f"{s.ollama_model}:latest"
        ms = round((time.perf_counter() - t0) * 1000, 1)
        if wanted not in names and s.ollama_model not in names:
            return ProviderStatus(
                "ollama", False, s.ollama_model,
                f"Model '{s.ollama_model}' not pulled. Run: ollama pull {s.ollama_model}", ms,
                s.runtime_for("ollama"),
            )
        status = ProviderStatus("ollama", True, s.ollama_model, "ok", ms, s.runtime_for("ollama"))
        # If the model is already loaded, Ollama reports its effective context window.
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=2.0)) as c:
                ps = (await c.get(f"{s.ollama_base_url.rstrip('/')}/api/ps")).json()
            for m in ps.get("models", []):
                if m.get("name") in (wanted, s.ollama_model) and m.get("context_length"):
                    if int(m["context_length"]) < s.ollama_min_context:
                        status.warning = (
                            f"Loaded with a {m['context_length']}-token context; prompts may be truncated. "
                            f"Set OLLAMA_CONTEXT_LENGTH={s.ollama_min_context} in Ollama's environment and restart it."
                        )
        except Exception:  # noqa: BLE001 — informational only
            pass
        return status
    except Exception as exc:  # noqa: BLE001
        return ProviderStatus(
            "ollama", False, s.ollama_model,
            f"Ollama unreachable at {s.ollama_base_url} ({type(exc).__name__})",
            round((time.perf_counter() - t0) * 1000, 1), s.runtime_for("ollama"),
        )


async def check_anthropic() -> ProviderStatus:
    s = get_settings()
    if not s.anthropic_api_key:
        return ProviderStatus(
            "anthropic", False, s.anthropic_model, "ANTHROPIC_API_KEY not set", None,
            s.runtime_for("anthropic"),
        )
    if not s.anthropic_api_key.startswith("sk-ant-"):
        return ProviderStatus(
            "anthropic", False, s.anthropic_model, "ANTHROPIC_API_KEY looks malformed", None,
            s.runtime_for("anthropic"),
        )
    # We deliberately do not call the API on every health check (cost/latency);
    # key presence + shape is the readiness signal. Real failures surface per request.
    return ProviderStatus("anthropic", True, s.anthropic_model, "ok", None, s.runtime_for("anthropic"))


async def check_all() -> list[ProviderStatus]:
    return [await check_anthropic(), await check_ollama()]


async def resolve(requested: Provider | None) -> tuple[ProviderStatus, bool]:
    """Pick a usable provider. Returns (status, fallback_used)."""
    s = get_settings()
    want: Provider = requested or s.llm_provider
    primary = await (check_anthropic() if want == "anthropic" else check_ollama())
    if primary.available:
        return primary, False
    if s.llm_fallback_provider and s.llm_fallback_provider != want:
        fb = await (check_anthropic() if s.llm_fallback_provider == "anthropic" else check_ollama())
        if fb.available:
            log.warning("provider_fallback", requested=want, used=fb.name, reason=primary.reason)
            return fb, True
        raise NoProviderAvailable([primary, fb])
    raise NoProviderAvailable([primary])


def make_client(provider: str) -> anthropic.AsyncAnthropic:
    s = get_settings()
    if provider == "anthropic":
        return anthropic.AsyncAnthropic(api_key=s.anthropic_api_key, timeout=s.llm_timeout_s, max_retries=1)
    return anthropic.AsyncAnthropic(
        base_url=s.ollama_base_url.rstrip("/"),
        api_key="ollama",
        timeout=s.llm_timeout_s,
        max_retries=0,
    )


def agent_sdk_env(provider: str) -> dict[str, str]:
    """Environment for the Claude Agent SDK subprocess, per provider."""
    s = get_settings()
    if provider == "anthropic":
        return {"ANTHROPIC_API_KEY": s.anthropic_api_key}
    return {"ANTHROPIC_BASE_URL": s.ollama_base_url.rstrip("/"), "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": ""}
