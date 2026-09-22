"""The memory's spare sense: embeddings computed on our own server, with no API at all.

Groq, the spare engine for answers, has no embedding model — measured on the key on 22 Sep: eleven
models, four for conversation, two for listening, two voices, three guards, none for memory. So the
search that finds what the group said cannot fall back to another company without one more account
that can also expire, be suspended or change its terms.

This runs the model in Jeli's own process instead: no key, no quota, no network. It cannot be rate
limited and it cannot go down while Jeli is up. It is the one part of Jeli that Google cannot take
away.

Its vectors live in their own space: a Gemini vector and a local vector are not comparable, so they
are stored in separate columns and a search uses the column matching whichever made the query
(see db/schema.sql and app/kb/store.py).
"""

import asyncio
import logging
import math
from collections.abc import Sequence

log = logging.getLogger(__name__)

# 384 dimensions, about 220 MB on disk, 50+ languages including French, English and Swahili.
# Measured 22 Sep: "La réunion est vendredi à 10h" and its English translation score 0.88 together,
# its Swahili one 0.56 — enough to find the right conversation.
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIMENSIONS = 384


def _normalize(values: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class LocalEmbedder:
    """The same shape as the Gemini embedder, computed here. Loaded in the background at startup:
    the first load downloads the model, and Jeli must answer while that happens."""

    space = "backup"
    dimensions = DIMENSIONS

    def __init__(self, model_name: str = MODEL):
        self.model_name = model_name
        self._model = None
        self._loading: asyncio.Lock | None = None
        self.failed = ""  # why it is unusable, for the dashboard ("" while it is fine)

    @property
    def ready(self) -> bool:
        return self._model is not None

    @property
    def available(self) -> bool:
        return not self.failed

    async def load(self) -> bool:
        """Bring the model into memory (downloading it the first time). False when it cannot be."""
        if self._model is not None:
            return True
        if self._loading is None:
            self._loading = asyncio.Lock()
        async with self._loading:
            if self._model is not None:
                return True
            try:
                from fastembed import TextEmbedding

                log.info("Local embeddings: loading %s (first time downloads it)", self.model_name)
                self._model = await asyncio.to_thread(TextEmbedding, self.model_name)
                log.info("Local embeddings ready: %d dimensions, no key and no quota", DIMENSIONS)
                return True
            except Exception as error:  # a missing model must never prevent the start
                self.failed = f"{type(error).__name__}: {error}"
                log.error("Local embeddings unavailable (%s): the memory has no spare sense", self.failed)
                return False

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        [vector] = await self._embed([text])
        return vector

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not await self.load():
            raise RuntimeError(f"Local embeddings unavailable: {self.failed}")
        rows = list(texts)
        vectors = await asyncio.to_thread(lambda: [v.tolist() for v in self._model.embed(rows)])
        return [_normalize(v) for v in vectors]
