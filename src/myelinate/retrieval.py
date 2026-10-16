"""Retrieval: BM25 (always on, portable) fused with dense similarity (when an embedder is configured)
via reciprocal-rank fusion, then MMR for diversity so near-duplicate items don't crowd the context."""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .embeddings import cosine

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have i in is it its of on or that the this to was were will with "
    "you your me my we our can could should would please do does how what when where which who why".split()
)


# Common abbreviations in operational text; both sides are normalised, so either spelling matches.
_ALIASES = {"k8s": "kubernetes", "prod": "production", "env": "environment", "db": "database", "repo": "repository",
            "config": "configuration", "auth": "authentication", "creds": "credentials", "infra": "infrastructure",
            "deps": "dependencies", "dev": "development", "pr": "pull request", "ci": "continuous integration"}
_SUFFIXES = ("ational", "ization", "ations", "ation", "ments", "ment", "ings", "ing", "edly", "ies", "ied", "ed",
             "es", "s")


def stem(t: str) -> str:
    """Light suffix stripping so deploy/deploying/deployed/deployment share a term (no NLP dependency)."""
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            base = t[: -len(suf)]
            return base + "y" if suf in ("ies", "ied") else base
    return t


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for t in _TOKEN.findall(text.lower()):
        for part in _ALIASES.get(t, t).split():
            if part not in _STOP and len(part) > 1:
                out.append(stem(part))
    return out


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize(a)), set(tokenize(b))
    return len(sa & sb) / len(sa | sb) if sa and sb else 0.0


def similarity(a: str, b: str, ea: Sequence[float] | None = None, eb: Sequence[float] | None = None) -> float:
    """Dense cosine when both embeddings exist, else lexical Jaccard."""
    return cosine(ea, eb) if ea and eb else jaccard(a, b)


class BM25:
    def __init__(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b

    def scores(self, query: str, docs: Sequence[str]) -> list[float]:
        """Normalised to [0, 1] relative to a strong match on every query term."""
        q = set(tokenize(query))
        if not q or not docs:
            return [0.0] * len(docs)
        toks = [tokenize(d) for d in docs]
        n = len(toks)
        avgdl = sum(len(t) for t in toks) / n or 1.0
        df = Counter(term for t in toks for term in set(t) if term in q)
        idf = {term: math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5)) for term in q}
        # Query words that appear in no document (names, versions, typos) carry little signal about which
        # document is relevant; down-weight them so they don't swamp small libraries.
        max_possible = sum(v if df[t] else 0.25 * v for t, v in idf.items()) * (self.k1 + 1) or 1.0
        out = []
        for t in toks:
            tf = Counter(t)
            dl = len(t) or 1
            s = sum(idf[term] * tf[term] * (self.k1 + 1) / (tf[term] + self.k1 * (1 - self.b + self.b * dl / avgdl))
                    for term in q if tf.get(term))
            out.append(min(1.0, s / max_possible * 2))
        return out


@dataclass
class Candidate:
    key: str
    text: str
    embedding: list[float] | None = None
    prior: float = 1.0  # multiplicative quality prior (e.g. skill utility)


@dataclass
class Hit:
    key: str
    score: float  # fused, prior-weighted
    relevance: float  # max(lexical, dense) in [0,1], used for thresholding


class HybridRetriever:
    def __init__(self, rrf_k: int = 60, mmr_lambda: float = 0.75) -> None:
        self.bm25 = BM25()
        self.rrf_k, self.mmr_lambda = rrf_k, mmr_lambda

    def search(self, query: str, cands: Sequence[Candidate], query_emb: list[float] | None, k: int,
               min_relevance: float) -> list[Hit]:
        if not cands or k <= 0:
            return []
        lex = self.bm25.scores(query, [c.text for c in cands])
        dense = [cosine(query_emb, c.embedding) if query_emb and c.embedding else 0.0 for c in cands]
        rel = [max(lx, d) for lx, d in zip(lex, dense, strict=True)]

        fused = [0.0] * len(cands)
        for scores in (lex, dense) if query_emb else (lex,):
            order = sorted(range(len(cands)), key=lambda i: scores[i], reverse=True)
            for rank, i in enumerate(order):
                if scores[i] > 0:
                    fused[i] += 1.0 / (self.rrf_k + rank + 1)
        top = max(fused) or 1.0
        base = [(f / top) * cands[i].prior for i, f in enumerate(fused)]

        pool = [i for i in range(len(cands)) if rel[i] >= min_relevance]
        chosen: list[int] = []
        while pool and len(chosen) < k:  # maximal marginal relevance
            def mmr(i: int) -> float:
                red = max((similarity(cands[i].text, cands[j].text, cands[i].embedding, cands[j].embedding)
                           for j in chosen), default=0.0)
                return self.mmr_lambda * base[i] - (1 - self.mmr_lambda) * red
            best = max(pool, key=mmr)
            chosen.append(best)
            pool.remove(best)
        return [Hit(cands[i].key, round(base[i], 4), round(rel[i], 4)) for i in chosen]
