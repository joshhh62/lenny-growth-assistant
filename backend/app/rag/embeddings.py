"""Embeddings via Ollama (`nomic-embed-text`, 768-d).

Design: embeddings are an *enhancement*, never a dependency. The background
worker embeds un-embedded chunks in batches whenever Ollama is reachable, and
retrieval degrades to lexical-only when it is not. This keeps the demo usable
seconds after ingest (30k chunks take ~20-40 min to embed on a CPU laptop).
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger("embeddings")


class EmbeddingUnavailable(RuntimeError):
    pass


class OllamaEmbedder:
    def __init__(self, base_url: str | None = None, model: str | None = None):
        s = get_settings()
        self.base_url = (base_url or s.ollama_base_url).rstrip("/")
        self.model = model or s.ollama_embed_model
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=3.0))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            r = await self._client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": texts, "truncate": True},
            )
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailable(f"Ollama unreachable at {self.base_url}: {exc}") from exc
        if r.status_code == 404:
            raise EmbeddingUnavailable(
                f"Embedding model '{self.model}' not found. Run: ollama pull {self.model}"
            )
        if r.status_code >= 400:
            raise EmbeddingUnavailable(f"Ollama embed error {r.status_code}: {r.text[:200]}")
        data = r.json()
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise EmbeddingUnavailable("Ollama returned a mismatched number of embeddings")
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        # nomic-embed-text recommends task prefixes.
        return (await self.embed([f"search_query: {text}"]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


class EmbeddingWorker:
    """Fills `chunks.embedding` in the background. Safe to restart at any time."""

    def __init__(self, sessionmaker, embedder: OllamaEmbedder):
        self.sessionmaker = sessionmaker
        self.embedder = embedder
        self._task: asyncio.Task | None = None
        self.state: dict = {"status": "idle", "embedded": 0, "error": None, "last_batch_ms": None}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="embedding-worker")

    async def stop(self, timeout_s: float = 3.0) -> None:
        """Cancel the worker; never block shutdown for more than `timeout_s`
        (a cancel that lands mid-query can otherwise wait on the driver)."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout_s)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                log.warning("embedding_worker_stop_timeout")

    async def _run(self) -> None:
        from app.db.repository import KnowledgeRepo

        s = get_settings()
        backoff = 5.0
        while True:
            try:
                async with self.sessionmaker() as db:
                    repo = KnowledgeRepo(db)
                    batch = await repo.unembedded_chunks(s.embed_batch_size)
                    if not batch:
                        self.state["status"] = "complete"
                        await asyncio.sleep(60)  # poll for newly ingested chunks
                        continue
                    t0 = time.perf_counter()
                    vecs = await self.embedder.embed(
                        [f"search_document: {c['text']}" for c in batch]
                    )
                    await repo.write_embeddings([(c["id"], v) for c, v in zip(batch, vecs)])
                    await db.commit()
                self.state["embedded"] += len(batch)
                self.state["status"] = "running"
                self.state["error"] = None
                self.state["last_batch_ms"] = round((time.perf_counter() - t0) * 1000)
                backoff = 5.0
                if self.state["embedded"] % (s.embed_batch_size * 20) == 0:
                    log.info("embedding_progress", **self.state)
            except asyncio.CancelledError:
                raise
            except EmbeddingUnavailable as exc:
                self.state["status"] = "waiting_for_ollama"
                self.state["error"] = str(exc)
                log.warning("embedding_paused", error=str(exc))
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as exc:  # noqa: BLE001
                self.state["status"] = "error"
                self.state["error"] = str(exc)[:300]
                log.exception("embedding_worker_error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)
