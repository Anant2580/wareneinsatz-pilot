"""Sentence embeddings from a local Ollama model, for matching by meaning.

Fuzzy matching compares letters: "Espresso ganze Bohne" and "Kaffeebohnen" share few. An
embedding model maps both to vectors that point in a similar direction, because it has learnt
what the words mean, also across languages ("Onion Rings" ~ "Zwiebelringe").

Default model: qwen3-embedding:0.6b (multilingual, 640 MB, free via Ollama). Vectors are
normalised once, so cosine similarity is a plain dot product.
"""

from __future__ import annotations

import math
from typing import Protocol

import httpx

from wareneinsatz.reading.llm import LlmUnavailable

Vector = list[float]


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[Vector]: ...


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-embedding:0.6b",
        *,
        timeout: float = 120,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self._http = httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=5.0), transport=transport
        )
        self._cache: dict[str, Vector] = {}  # the same ingredient names are embedded again and again

    def embed(self, texts: list[str]) -> list[Vector]:
        missing = list(dict.fromkeys(text for text in texts if text not in self._cache))
        for start in range(0, len(missing), 64):  # batches keep each request small
            batch = missing[start : start + 64]
            try:
                response = self._http.post("/api/embed", json={"model": self.model, "input": batch})
            except httpx.HTTPError as error:
                raise LlmUnavailable(f"Ollama is not reachable at {self.base_url} ({error})") from error
            if response.status_code == 404:
                raise LlmUnavailable(f"The model {self.model} is not downloaded. Run: ollama pull {self.model}")
            response.raise_for_status()
            for text, vector in zip(batch, response.json()["embeddings"], strict=True):
                self._cache[text] = normalise(vector)
        return [self._cache[text] for text in texts]


def normalise(vector: Vector) -> Vector:
    length = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / length for x in vector]


def cosine(a: Vector, b: Vector) -> float:
    """Both vectors are normalised: the dot product is the cosine similarity."""
    return sum(x * y for x, y in zip(a, b, strict=True))
