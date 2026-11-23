"""Generate text embeddings via OpenAI."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import List, Optional

from openai import OpenAI

from config import settings

logger = logging.getLogger(__name__)

_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = settings.openai_api_key
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required for embeddings")
        _client = OpenAI(api_key=api_key)
    return _client


def _embed_text_uncached(cleaned: str, dimensions: Optional[int] = None) -> List[float]:
    dims = int(dimensions or settings.embedding_dimensions)
    client = _get_client()
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=cleaned,
        dimensions=dims,
    )

    if not response.data:
        raise ValueError("Embedding API returned no vector")

    vector = [float(v) for v in response.data[0].embedding]
    if len(vector) != dims:
        raise ValueError(f"Expected {dims} dimensions, got {len(vector)}")
    return vector


@lru_cache(maxsize=2048)
def _embed_text_cached(cleaned: str, dimensions: int) -> tuple[float, ...]:
    return tuple(_embed_text_uncached(cleaned, dimensions))


def embed_text(text: str, dimensions: Optional[int] = None) -> List[float]:
    """Return a fixed-size embedding vector for the given text."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("Cannot embed empty text")
    dims = int(dimensions or settings.embedding_dimensions)
    return list(_embed_text_cached(cleaned, dims))
