"""Semantic cache for the Accountant wrapper.

A tool call (e.g. web_search) is served from cache only when its query
is *semantically equivalent* to a previously executed one — cosine
similarity of embeddings ≥ threshold. This is the quality guardrail
that makes runtime suppression safe: we never substitute a cached
result for a materially different request.

The win is cross-ticket: a support pipeline fires the same external
lookups ("current FTC refund regulations", …) on every refund ticket.
After the first, they're cache hits — the real (expensive) call never
executes — while equivalence keeps the served context correct.

In-memory and per-tool for the demo (the gateway's local stand-in); a
production gateway would back this with a vector store like Redis.
"""

import math
import os
import threading

from google import genai


EMBED_MODEL = os.environ.get("ACCOUNTANT_EMBED_MODEL", "text-embedding-005")
# Cosine similarity at/above which two queries count as equivalent.
# 0.93 keeps near-duplicates together while rejecting genuinely
# different intents. Tunable per policy.
DEFAULT_THRESHOLD = float(os.environ.get("ACCOUNTANT_CACHE_THRESHOLD", "0.93"))


_client: genai.Client | None = None

# Memoize embeddings by exact text. The same query strings recur
# constantly (every refund fires the same 3 searches), so this collapses
# embedding API calls to one per unique string — correctness-free (an
# identical string always embeds to the same vector) and a big cut in
# per-minute API load (avoids 429s).
_embed_cache: dict[str, list[float]] = {}
_embed_lock = threading.Lock()


def _genai_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def embed(text: str) -> list[float]:
    with _embed_lock:
        cached = _embed_cache.get(text)
    if cached is not None:
        return cached
    resp = _genai_client().models.embed_content(model=EMBED_MODEL, contents=text)
    vec = list(resp.embeddings[0].values)
    with _embed_lock:
        _embed_cache[text] = vec
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class CacheHit:
    __slots__ = ("result", "similarity", "matched_query")

    def __init__(self, result, similarity: float, matched_query: str):
        self.result = result
        self.similarity = similarity
        self.matched_query = matched_query


class SemanticCache:
    """Per-tool semantic cache. Thread-safe (the wrapper may be called
    from concurrent agent runs)."""

    def __init__(self, threshold: float = DEFAULT_THRESHOLD):
        self._threshold = threshold
        self._lock = threading.Lock()
        # tool -> list of (query, embedding, result)
        self._store: dict[str, list[tuple[str, list[float], object]]] = {}

    def lookup(self, tool: str, query: str) -> CacheHit | None:
        """Return a CacheHit if a stored query for this tool is
        semantically equivalent (>= threshold), else None."""
        try:
            q_emb = embed(query)
        except Exception:
            return None  # fail open: on embed error, don't suppress
        with self._lock:
            entries = self._store.get(tool, [])
            best = None
            best_sim = 0.0
            for cached_query, emb, result in entries:
                sim = cosine(q_emb, emb)
                if sim > best_sim:
                    best_sim, best = sim, (cached_query, result)
        if best and best_sim >= self._threshold:
            return CacheHit(result=best[1], similarity=best_sim, matched_query=best[0])
        return None

    def store(self, tool: str, query: str, result) -> None:
        try:
            q_emb = embed(query)
        except Exception:
            return
        with self._lock:
            self._store.setdefault(tool, []).append((query, q_emb, result))

    def stats(self) -> dict:
        with self._lock:
            return {tool: len(v) for tool, v in self._store.items()}
