"""Text embeddings with Gemini (gemini-embedding-001)."""

import asyncio
import logging
import math
from collections.abc import Sequence

from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

MODEL = "gemini-embedding-001"
# Must match the jeli.chunks.embedding column, vector(768). Changing it means re-indexing everything.
DIMENSIONS = 768
BATCH_SIZE = 100
RETRY_DELAYS = (2, 5, 10, 20, 40)  # seconds, on rate limits and server errors


def _normalize(values: Sequence[float]) -> list[float]:
    """Below 3,072 dimensions Gemini embeddings are not unit vectors: normalise for cosine search."""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class Embedder:
    def __init__(self, api_key: str, client: genai.Client | None = None):
        self._client = client or genai.Client(api_key=api_key)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([text], "RETRIEVAL_QUERY")
        return vector

    async def _embed(self, texts: Sequence[str], task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMENSIONS)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            response = await self._call_with_retry(batch, config)
            vectors.extend(_normalize(embedding.values) for embedding in response.embeddings)
        return vectors

    async def _call_with_retry(self, batch: list[str], config: types.EmbedContentConfig):
        for delay in (*RETRY_DELAYS, None):
            try:
                return await self._client.aio.models.embed_content(model=MODEL, contents=batch, config=config)
            except errors.APIError as error:
                retryable = error.code == 429 or (error.code or 0) >= 500
                if not retryable or delay is None:
                    raise
                log.warning("Gemini embeddings returned %s, retrying in %s s", error.code, delay)
                await asyncio.sleep(delay)
