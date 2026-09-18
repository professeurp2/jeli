"""Text embeddings with Gemini (gemini-embedding-001)."""

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Sequence

from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

MODEL = "gemini-embedding-001"
# Must match the jeli.chunks.embedding column, vector(768). Changing it means re-indexing everything.
DIMENSIONS = 768
BATCH_SIZE = 100  # texts per request, the API maximum
# The free tier caps tokens per minute (about 30k): one batch of 100 conversation chunks already
# reaches it. Requests are kept small and the per-minute budget below that cap.
BATCH_TOKENS = 8_000
TOKENS_PER_MINUTE = 25_000
CHARS_PER_TOKEN = 3  # conservative for mixed French/English chat text
RETRY_DELAYS = (10, 30, 60, 60, 60)  # seconds, on rate limits and server errors


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _normalize(values: Sequence[float]) -> list[float]:
    """Below 3,072 dimensions Gemini embeddings are not unit vectors: normalise for cosine search."""
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _batches(texts: Sequence[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    tokens = 0
    for text in texts:
        cost = estimate_tokens(text)
        if current and (len(current) == BATCH_SIZE or tokens + cost > BATCH_TOKENS):
            batches.append(current)
            current, tokens = [], 0
        current.append(text)
        tokens += cost
    if current:
        batches.append(current)
    return batches


class TokenBudget:
    """Sliding one-minute budget: waits before a request that would exceed it."""

    def __init__(self, tokens_per_minute: int, clock: Callable[[], float] = time.monotonic, sleep=asyncio.sleep):
        self.limit = tokens_per_minute
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()

    async def spend(self, tokens: int) -> None:
        while True:
            now = self._clock()
            while self._spent and now - self._spent[0][0] >= 60:
                self._spent.popleft()
            used = sum(cost for _, cost in self._spent)
            if not self._spent or used + tokens <= self.limit:
                self._spent.append((now, tokens))
                return
            await self._sleep(60 - (now - self._spent[0][0]))


class Embedder:
    def __init__(self, api_key: str, client: genai.Client | None = None, tokens_per_minute: int = TOKENS_PER_MINUTE):
        self._client = client or genai.Client(api_key=api_key)
        self._budget = TokenBudget(tokens_per_minute)

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([text], "RETRIEVAL_QUERY")
        return vector

    async def _embed(self, texts: Sequence[str], task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMENSIONS)
        vectors: list[list[float]] = []
        for batch in _batches(texts):
            await self._budget.spend(sum(estimate_tokens(text) for text in batch))
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
