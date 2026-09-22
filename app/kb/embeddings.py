"""Text embeddings with Gemini (gemini-embedding-001)."""

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Sequence

import httpx
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
    """Gemini's embeddings, with the local model as a spare sense (app/kb/local_embeddings.py).

    The two live in different spaces: what is written to one column cannot be searched in the
    other. Every passage is therefore embedded in both, and a question is searched in the space of
    whichever embedder answered it."""

    space = "gemini"

    def __init__(
        self,
        api_keys: list[str] | str,
        client: genai.Client | None = None,
        tokens_per_minute: int = TOKENS_PER_MINUTE,
        backup=None,
    ):
        self.backup = backup
        if client:
            self._clients = [client]
        else:
            keys = [api_keys] if isinstance(api_keys, str) else api_keys
            self._clients = [genai.Client(api_key=k) for k in keys if k]
        self._next_key = 0  # round-robin cursor: advanced after each batch
        # Combined budget: n keys × per-key quota = effective tokens per minute.
        self._budget = TokenBudget(tokens_per_minute * len(self._clients))

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([text], "RETRIEVAL_QUERY")
        return vector

    async def embed_both(self, texts: Sequence[str]) -> tuple[list[list[float]] | None, list[list[float]] | None]:
        """A passage in both spaces, so it can be found whichever embedder is working when the
        question comes. Either side may be None when that embedder could not be reached."""
        try:
            main = await self.embed_documents(texts)
        except Exception as error:
            log.error("Gemini embeddings failed (%s): this passage is only in the spare space", error)
            main = None
        spare = None
        if self.backup is not None and self.backup.available:
            try:
                spare = await self.backup.embed_documents(texts)
            except Exception as error:
                log.warning("Local embeddings failed (%s)", error)
        return main, spare

    async def query(self, text: str) -> tuple[list[float], str]:
        """The question as a vector, and which space it belongs to. Gemini first; when it cannot,
        the local model — which needs no key and cannot be rate limited."""
        try:
            return await self.embed_query(text), self.space
        except Exception as error:
            if self.backup is None or not self.backup.available:
                raise
            log.warning("Gemini could not embed the question (%s): searching the spare memory", error)
            return await self.backup.embed_query(text), self.backup.space

    async def _embed(self, texts: Sequence[str], task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMENSIONS)
        vectors: list[list[float]] = []
        for batch in _batches(texts):
            await self._budget.spend(sum(estimate_tokens(text) for text in batch))
            response = await self._call_with_retry(batch, config)
            vectors.extend(_normalize(embedding.values) for embedding in response.embeddings)
        return vectors

    async def _call_with_retry(self, batch: list[str], config: types.EmbedContentConfig):
        keys_tried: set[int] = set()
        delays = iter((*RETRY_DELAYS, None))
        key = self._next_key  # start from the round-robin cursor
        while True:
            try:
                result = await self._clients[key].aio.models.embed_content(model=MODEL, contents=batch, config=config)
                self._next_key = (key + 1) % len(self._clients)  # advance after success
                return result
            except (errors.APIError, httpx.TransportError) as error:
                code = getattr(error, "code", None)
                if code in (401, 429):
                    if code == 401:
                        log.error("Key %d rejected with 401 UNAUTHENTICATED — verify it is valid and Gemini API is enabled", key)
                    keys_tried.add(key)
                    if len(keys_tried) < len(self._clients):
                        old_key = key
                        key = (key + 1) % len(self._clients)
                        log.info("Embeddings rotating from key %d to key %d (%s)", old_key, key, code)
                        continue  # retry immediately with next key
                retryable = isinstance(error, httpx.TransportError) or code == 429 or (code or 0) >= 500
                delay = next(delays)
                if not retryable or delay is None:
                    raise
                log.warning("Gemini embeddings failed (%s), retrying in %s s", code or type(error).__name__, delay)
                keys_tried.clear()  # reset after waiting so rotation can restart
                key = self._next_key  # resume from round-robin position after wait
                await asyncio.sleep(delay)
