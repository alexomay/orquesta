"""Embeddings para búsqueda semántica.

Es opcional: con VOYAGE_API_KEY (y `pip install -e ".[rag]"`) se usa Voyage AI, el proveedor
de embeddings que recomienda Anthropic. Sin key, la búsqueda queda solo en BM25.
"""

import math
from typing import Protocol


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        """kind = 'document' al indexar, 'query' al buscar."""
        ...


class VoyageEmbedder:
    name = "voyage-3.5"

    def __init__(self, api_key: str) -> None:
        import voyageai  # dependencia opcional

        self.client = voyageai.AsyncClient(api_key=api_key)

    async def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        result = await self.client.embed(texts, model=self.name, input_type=kind)
        return result.embeddings


def build_embedder(api_key: str | None) -> Embedder | None:
    if not api_key:
        return None
    try:
        return VoyageEmbedder(api_key)
    except ImportError:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
