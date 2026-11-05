from app.rag import KBDoc, Retriever
from app.rag.bm25 import BM25
from app.rag.eval import EVAL_SET, evaluate
from app.seed import KB
from app.text import stem, tokenize


async def kb_retriever(embedder=None) -> Retriever:
    r = Retriever(embedder)
    await r.index([KBDoc(i, t, tags, c) for i, (t, tags, c) in enumerate(KB, 1)])
    return r


def test_tokenizer_normalizes_accents_stopwords_and_plurals():
    assert tokenize("¿La Contraseña de mis CUENTAS?") == ["contrasen", "cuent"]
    assert stem("impresoras") == stem("impresora")


def test_bm25_prefers_rare_terms():
    docs = [["vpn", "red"], ["red", "wifi"], ["red", "impresora"]]
    scores = BM25(docs).scores(["vpn", "red"])
    assert scores[0] > scores[1] and scores[0] > scores[2]


async def test_bm25_quality_gate():
    """Si un cambio empeora la búsqueda, la CI falla."""
    report = await evaluate(await kb_retriever())
    assert report.total == len(EVAL_SET)
    assert report.recall_at_3 >= 0.95, report.misses
    assert report.mrr >= 0.9


class KeywordEmbedder:
    """Embedder de prueba: un vector por concepto, para verificar la fusión sin red."""

    name = "test-embedder"
    concepts = ["casa", "clave", "nube"]
    synonyms = {
        "casa": ["vpn", "remoto"],
        "clave": ["contrasena", "password"],
        "nube": ["onedrive"],
    }

    async def embed(self, texts, kind):
        out = []
        for text in texts:
            toks = set(tokenize(text)) | {t for t in text.lower().split()}
            out.append(
                [
                    1.0 if c in toks or any(s in toks for s in self.synonyms[c]) else 0.01
                    for c in self.concepts
                ]
            )
        return out


async def test_hybrid_fuses_both_rankings():
    r = await kb_retriever(KeywordEmbedder())
    assert r.mode.startswith("hybrid")
    hits = await r.search("mis archivos no se suben a la nube", k=3)
    assert hits[0].title == "OneDrive no sincroniza archivos"
    assert set(hits[0].ranks) == {"bm25", "vector"}
    assert hits == sorted(hits, key=lambda h: -h.score)


async def test_empty_index_returns_nothing():
    assert await Retriever().search("vpn") == []
