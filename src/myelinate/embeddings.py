"""Optional dense embeddings. Any endpoint speaking the common `/v1/embeddings` JSON shape works
(Voyage AI, Azure AI, TEI, vLLM, Ollama, LiteLLM proxy...). Without one, retrieval is lexical only."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx


class Embedder(Protocol):
    dim: int | None

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class HttpEmbedder:
    def __init__(self, url: str, model: str, api_key: str | None = None, batch: int = 64, timeout: float = 30.0):
        self.url, self.model, self.batch = url, model, batch
        self.dim: int | None = None
        headers = {"authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(headers=headers, timeout=timeout)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            r = await self._http.post(self.url, json={"model": self.model, "input": list(texts[i : i + self.batch])})
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            out.extend(_normalise(d["embedding"]) for d in data)
        if out:
            self.dim = len(out[0])
        return out


def _normalise(v: Sequence[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True))  # inputs are unit-normalised
