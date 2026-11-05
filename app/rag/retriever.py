"""Recuperación híbrida para la base de conocimiento.

BM25 encuentra coincidencias exactas de palabras ("FortiClient", "SAP"); los embeddings
encuentran sinónimos y paráfrasis ("no puedo entrar desde casa" ~ "VPN"). Los dos rankings se
combinan con Reciprocal Rank Fusion (RRF), que no necesita calibrar puntajes de escalas
distintas.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..models import KBArticle
from ..text import tokenize
from .bm25 import BM25
from .embeddings import Embedder, cosine

RRF_K = 60  # valor estándar de la literatura (Cormack et al., 2009)


@dataclass
class KBDoc:
    id: int
    title: str
    tags: str
    content: str

    def text_for_index(self) -> str:
        # Título y etiquetas pesan doble.
        return f"{self.title} {self.title} {self.tags} {self.tags} {self.content}"


@dataclass
class Hit:
    id: int
    title: str
    content: str
    score: float
    ranks: dict[str, int] = field(default_factory=dict)


class Retriever:
    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder
        self.docs: list[KBDoc] = []
        self._bm25: BM25 | None = None
        self._vectors: list[list[float]] = []

    @property
    def mode(self) -> str:
        return f"hybrid (bm25 + {self.embedder.name})" if self.embedder else "bm25"

    async def index(self, docs: list[KBDoc]) -> None:
        self.docs = docs
        self._bm25 = BM25([tokenize(d.text_for_index()) for d in docs])
        self._vectors = []
        if self.embedder and docs:
            self._vectors = await self.embedder.embed(
                [f"{d.title}\n{d.content}" for d in docs], "document"
            )

    async def index_from_db(self, sessions: async_sessionmaker) -> None:
        async with sessions() as s:
            rows = (await s.scalars(select(KBArticle).order_by(KBArticle.id))).all()
        await self.index([KBDoc(r.id, r.title, r.tags, r.content) for r in rows])

    async def search(self, query: str, k: int = 3) -> list[Hit]:
        if not self.docs or self._bm25 is None:
            return []
        rankings: dict[str, list[int]] = {}

        bm25 = self._bm25.scores(tokenize(query))
        rankings["bm25"] = [
            i for i in sorted(range(len(bm25)), key=lambda i: -bm25[i]) if bm25[i] > 0
        ]

        if self.embedder and self._vectors:
            [qv] = await self.embedder.embed([query], "query")
            sims = [cosine(qv, v) for v in self._vectors]
            rankings["vector"] = sorted(range(len(sims)), key=lambda i: -sims[i])[: k * 3]

        fused: dict[int, float] = {}
        ranks: dict[int, dict[str, int]] = {}
        for method, order in rankings.items():
            for rank, i in enumerate(order, start=1):
                fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank)
                ranks.setdefault(i, {})[method] = rank

        best = sorted(fused, key=lambda i: -fused[i])[:k]
        return [
            Hit(
                self.docs[i].id,
                self.docs[i].title,
                self.docs[i].content,
                round(fused[i], 5),
                ranks[i],
            )
            for i in best
        ]
