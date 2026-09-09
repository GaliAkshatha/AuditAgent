"""
AuditAgent v2 — Retrieval
Lightweight TF-IDF retrieval over the pattern corpus (corpus.py). No
embedding API call needed — the corpus is small (~20 patterns) and plain
term-frequency matching against tags + problem text is sufficient at this
scale. Per the project plan: upgrade to GraphRAG only once the corpus
grows large enough that concept relationships actually affect retrieval
quality — not needed yet.
"""

import math
import re
from collections import Counter

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "in", "on", "at", "for", "with", "this",
    "that", "it", "its", "as", "by", "from", "has", "have", "had",
    "will", "would", "could", "should", "not", "no", "if", "than",
}


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


def _doc_text(pattern: dict) -> str:
    return " ".join([pattern["title"], pattern["problem"], " ".join(pattern["tags"])])


class TfidfIndex:
    def __init__(self, corpus: list[dict]):
        self.corpus = corpus
        self.doc_tokens = [tokenize(_doc_text(p)) for p in corpus]
        self.doc_freq = self._compute_doc_freq()
        self.n_docs = len(corpus)
        self.doc_vectors = [self._tfidf_vector(tokens) for tokens in self.doc_tokens]

    def _compute_doc_freq(self) -> Counter:
        df = Counter()
        for tokens in self.doc_tokens:
            for term in set(tokens):
                df[term] += 1
        return df

    def _tfidf_vector(self, tokens: list[str]) -> dict[str, float]:
        tf = Counter(tokens)
        vec = {}
        for term, count in tf.items():
            df = self.doc_freq.get(term, 0)
            if df == 0:
                continue
            idf = math.log((1 + self.n_docs) / (1 + df)) + 1
            vec[term] = count * idf
        return vec

    @staticmethod
    def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
        common = set(v1) & set(v2)
        if not common:
            return 0.0
        dot = sum(v1[t] * v2[t] for t in common)
        norm1 = math.sqrt(sum(x * x for x in v1.values()))
        norm2 = math.sqrt(sum(x * x for x in v2.values()))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot / (norm1 * norm2)

    def query(self, text: str, top_k: int = 3, min_score: float = 0.05) -> list[dict]:
        query_tokens = tokenize(text)
        # Query terms weighted by idf only (no repeated-term boost needed
        # for short queries built from structured findings).
        query_vec = {}
        for term in set(query_tokens):
            df = self.doc_freq.get(term, 0)
            if df == 0:
                continue
            idf = math.log((1 + self.n_docs) / (1 + df)) + 1
            query_vec[term] = idf

        scored = []
        for i, doc_vec in enumerate(self.doc_vectors):
            score = self._cosine(query_vec, doc_vec)
            if score >= min_score:
                scored.append((score, self.corpus[i]))

        scored.sort(key=lambda x: -x[0])
        return [{"score": round(s, 4), **p} for s, p in scored[:top_k]]
