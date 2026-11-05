"""BM25 (Okapi): ranking léxico clásico, el mismo principio que usan Elasticsearch y Lucene."""

import math
from collections import Counter


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lengths = [len(d) for d in docs]
        self.avgdl = sum(self.lengths) / self.n if self.n else 0.0
        self.tf = [Counter(d) for d in docs]
        df = Counter(term for d in docs for term in set(d))
        # IDF con suavizado (+1) para que nunca sea negativo.
        self.idf = {t: math.log(1 + (self.n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, length in zip(self.tf, self.lengths, strict=True):
            score = 0.0
            for term in set(query):
                freq = tf.get(term)
                if not freq:
                    continue
                norm = 1 - self.b + self.b * length / (self.avgdl or 1)
                score += self.idf[term] * freq * (self.k1 + 1) / (freq + self.k1 * norm)
            out.append(score)
        return out
