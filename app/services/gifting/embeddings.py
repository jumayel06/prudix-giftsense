"""Embeddings for gift matching (docs/TECHNICAL_PLAN.md §4.3).

Production: OpenAI text-embedding-3-small at 512 dimensions (already used by
Commerce; Anthropic has no embeddings endpoint). Tests and offline dev: the
deterministic FakeEmbedder, so rankings are reproducible with no API calls.
"""
import hashlib
import math
import re
from typing import Protocol, Sequence

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMS = 512


class Embedder(Protocol):
    dims: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 for empty or mismatched vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class FakeEmbedder:
    """Hashed bag-of-words → unit vector. Deterministic and offline: texts that
    share words are similar, which is enough to test ranking logic."""

    def __init__(self, dims: int = EMBEDDING_DIMS):
        self.dims = dims

    def embed_sync(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        for token in re.findall(r"[a-z0-9]+", (text or "").lower()):
            h = hashlib.md5(token.encode()).digest()
            idx = int.from_bytes(h[:4], "big") % self.dims
            vec[idx] += 1.0 if h[4] % 2 else 0.8
        return _normalize(vec)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_sync(t) for t in texts]


class OpenAIEmbedder:
    """text-embedding-3-small, batched. ~$0.02 per million tokens."""

    def __init__(self, dims: int = EMBEDDING_DIMS, model: str = EMBEDDING_MODEL):
        self.dims = dims
        self.model = model

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from app.llm import _openai_client
        if not texts:
            return []
        resp = await _openai_client.embeddings.create(model=self.model, input=texts, dimensions=self.dims)
        return [list(d.embedding) for d in sorted(resp.data, key=lambda d: d.index)]
