"""Integration tests (Postgres + fake LLM): API contracts, persistence, routing, resilience."""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration


def parse_sse(raw: str) -> list[dict]:
    events = []
    for block in raw.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


async def chat(client, session_id: str, content: str) -> list[dict]:
    async with client.stream("POST", f"/api/sessions/{session_id}/messages", json={"content": content}) as r:
        assert r.status_code == 200, await r.aread()
        assert r.headers["content-type"].startswith("text/event-stream")
        body = (await r.aread()).decode()
    return parse_sse(body)


# --- health & config ---------------------------------------------------------
async def test_health_and_readiness(client):
    assert (await client.get("/health")).json()["status"] == "ok"
    ready = (await client.get("/health/ready")).json()
    assert ready["status"] == "ready"
    assert ready["database"]["ok"] and ready["database"]["episodes"] == 2  # fixture dedupes 3 → 2
    provs = {p["name"]: p for p in ready["providers"]}
    assert provs["ollama"]["available"] and provs["ollama"]["runtime"] == "messages_loop"
    assert not provs["anthropic"]["available"] and "ANTHROPIC_API_KEY" in provs["anthropic"]["reason"]


async def test_config_exposes_provider_toggle(client):
    cfg = (await client.get("/api/config")).json()
    assert cfg["default_provider"] == "ollama"
    assert cfg["retrieval"]["episodes"] == 2 and cfg["retrieval"]["chunks"] > 0


# --- contracts ---------------------------------------------------------------
async def test_validation_errors_are_structured(client):
    r = await client.post("/api/sessions", json={"provider": "gpt-9"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error" and body["error"]["request_id"]
    r = await client.post("/api/sessions/00000000-0000-0000-0000-000000000000/messages", json={"content": ""})
    assert r.status_code == 422


async def test_not_found_is_structured(client):
    r = await client.get("/api/sessions/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404 and r.json()["error"]["code"] == "session_not_found"


# --- sessions & persistence --------------------------------------------------
async def test_session_lifecycle_and_user_metadata(client):
    r = await client.post("/api/sessions", json={"user_id": "u1", "display_name": "Josh", "metadata": {"tz": "IST"}})
    assert r.status_code == 201
    s = r.json()
    assert s["provider"] == "ollama" and s["model"] == "fake-model" and s["title"] == "New chat"
    assert s["metadata"]["tz"] == "IST" and "user_agent" in s["metadata"]

    r = await client.patch(f"/api/sessions/{s['id']}", json={"title": "Renamed", "provider": "anthropic"})
    assert r.json()["title"] == "Renamed" and r.json()["provider"] == "anthropic"

    listed = (await client.get("/api/sessions", params={"user_id": "u1"})).json()
    assert [x["id"] for x in listed] == [s["id"]]
    assert (await client.get("/api/sessions", params={"user_id": "someone-else"})).json() == []

    assert (await client.delete(f"/api/sessions/{s['id']}")).status_code == 204
    assert (await client.get(f"/api/sessions/{s['id']}")).status_code == 404


async def test_sessions_keep_independent_context(client):
    a = (await client.post("/api/sessions", json={"user_id": "u1"})).json()["id"]
    b = (await client.post("/api/sessions", json={"user_id": "u1"})).json()["id"]
    await chat(client, a, "What does Elena Verna say about retention?")
    await chat(client, b, "What do PMs get wrong at startups?")
    da = (await client.get(f"/api/sessions/{a}")).json()
    db_ = (await client.get(f"/api/sessions/{b}")).json()
    assert [m["role"] for m in da["messages"]] == ["user", "assistant"]
    assert [m["role"] for m in db_["messages"]] == ["user", "assistant"]
    assert da["messages"][0]["content"].startswith("What does Elena")
    assert db_["messages"][0]["content"].startswith("What do PMs")
    assert da["session"]["title"] == "What does Elena Verna say about retention?"


# --- grounded chat -----------------------------------------------------------
async def test_chat_streams_grounded_answer_with_citations(client):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "Why is retention the foundation of growth?")
    types = [e["type"] for e in events]
    assert types[0] == "provider" and events[0]["provider"] == "ollama" and events[0]["fallback_used"] is False
    assert "citations" in types and "token" in types and types[-1] == "done"
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "[1]" in text

    final_cits = [e for e in events if e["type"] == "citations" and e.get("final")][-1]["items"]
    assert final_cits and final_cits[0]["guest"] == "Elena Verna"
    assert final_cits[0]["timestamp_url"].startswith("https://www.youtube.com/watch?v=TESTVID1&t=")

    detail = (await client.get(f"/api/sessions/{sid}")).json()
    assistant = detail["messages"][-1]
    assert assistant["citations"][0]["episode_id"] == "elena-verna"
    assert assistant["provider"] == "ollama" and assistant["runtime"] == "messages_loop"
    assert assistant["latency_ms"] is not None and assistant["input_tokens"] == 100


async def test_model_can_call_search_tool_mid_turn(client, fake_llm):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "USE_TOOL:search tell me about cohorts")
    names = [e["name"] for e in events if e["type"] == "tool_call"]
    assert names == ["search_transcripts"]
    assert any(e["type"] == "tool_result" for e in events)
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "After searching again" in text
    # The tool result was fed back to the model as a tool_result block.
    last_call = fake_llm.calls[-1]
    assert any(b.get("type") == "tool_result" for b in last_call["messages"][-1]["content"])
    detail = (await client.get(f"/api/sessions/{sid}")).json()
    assert detail["messages"][-1]["tool_calls"][0]["name"] == "search_transcripts"


async def test_follow_up_carries_history(client, fake_llm):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    await chat(client, sid, "How does Elena Verna define activation?")
    await chat(client, sid, "and habit loops?")
    call = fake_llm.calls[-1]
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert "activation" in call["messages"][0]["content"].lower()


# --- skills: essay & artifacts --------------------------------------------------
async def test_essay_route_creates_markdown_artifact(client):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "Write a Ship 30 for 30 essay about retention")
    arts = [e for e in events if e["type"] == "artifact"]
    assert len(arts) == 1 and arts[0]["kind"] == "markdown"
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["write_ship30_essay"]
    art = (await client.get(f"/api/artifacts/{arts[0]['id']}")).json()
    assert art["content"].startswith("# 5 Retention Lessons")
    assert "### Sources" in art["content"] and "Elena Verna" in art["content"]
    done = events[-1]
    assert done["type"] == "done" and done["artifacts"] == [arts[0]["id"]]
    detail = (await client.get(f"/api/sessions/{sid}")).json()
    assert detail["artifacts"][0]["message_id"] == detail["messages"][-1]["id"]


async def test_html_artifact_is_sanitized_on_write(client):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "Create an HTML one-pager about growth loops")
    art_ev = next(e for e in events if e["type"] == "artifact")
    assert art_ev["kind"] == "html" and art_ev["title"] == "Growth Loops One-Pager"
    assert "script" in art_ev["sanitize_report"]["removed_tags"]
    html = (await client.get(f"/api/artifacts/{art_ev['id']}")).json()["content"]
    assert "<script" not in html and "document.cookie" not in html
    assert "onclick" not in html and "javascript:" not in html and "<iframe" not in html
    assert "evil.example" not in html
    assert "Content-Security-Policy" in html and "<h1>Growth Loops</h1>" in html
    assert "max-width:760px" in html  # legitimate CSS survives


async def test_markdown_artifact_route(client):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "Make a markdown checklist for user interviews")
    art_ev = next(e for e in events if e["type"] == "artifact")
    assert art_ev["kind"] == "markdown" and art_ev["title"] == "User Interview Checklist"


# --- retrieval ---------------------------------------------------------------
async def test_search_endpoint_hybrid_and_deep_links(client):
    r = await client.get("/api/search", params={"q": "leaky bucket retention", "k": 3})
    d = r.json()
    assert d["hits"] and d["hits"][0]["guest"] == "Elena Verna"
    assert d["hits"][0]["timestamp_url"].endswith("&t=5s")  # chunk starts at the question that precedes the answer
    assert d["mode"] in ("lexical", "hybrid")


async def test_empty_retrieval_is_acknowledged(client, fake_llm):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "zzqxv nonsense quantum kittens")
    assert events[-1]["type"] == "done"
    prompt = fake_llm.calls[-1]["messages"][-1]["content"]
    assert "no transcript passages matched" in prompt


# --- resilience ---------------------------------------------------------------
async def test_model_timeout_is_reported_and_persisted(client):
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "FAIL_TIMEOUT please")
    err = events[-1]
    assert err["type"] == "error" and err["code"] == "timeout" and err["retryable"]
    detail = (await client.get(f"/api/sessions/{sid}")).json()
    assert detail["messages"][-1]["error"] == "timeout"


async def test_no_provider_available(client, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "ollama_base_url", "http://127.0.0.1:9")  # nothing listens
    sid = (await client.post("/api/sessions", json={})).json()["id"]
    events = await chat(client, sid, "hello")
    assert events[-1]["type"] == "error" and events[-1]["code"] == "no_provider"
    assert "unreachable" in events[-1]["message"]


async def test_missing_model_gives_actionable_reason(client, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ollama_model", "not-pulled:7b")
    provs = {p["name"]: p for p in (await client.get("/health/providers")).json()}
    assert not provs["ollama"]["available"] and "ollama pull not-pulled:7b" in provs["ollama"]["reason"]


async def test_database_down_returns_503(client, monkeypatch):
    from app.db import database

    async def boom():
        raise database.DatabaseUnavailable("connection refused")

    monkeypatch.setattr(database, "get_db", boom)
    from app.main import app as _  # noqa: F401 — dependency override path

    # Simulate by breaking the engine URL for a new dependency call.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "database_url", "postgresql+asyncpg://x:x@127.0.0.1:9/x")
    database._engine = None
    database._sessionmaker = None
    r = await client.get("/api/config")
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"
