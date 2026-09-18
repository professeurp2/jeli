"""Background job: index the live messages every few minutes."""

import asyncio
import logging

from app.ingest.chunker import MAX_GAP
from app.kb.embeddings import Embedder
from app.kb.indexer import index_pending
from app.kb.store import Store

log = logging.getLogger(__name__)


async def index_periodically(store: Store, embedder: Embedder, interval_seconds: int) -> None:
    while True:
        try:
            # A conversation is indexed once it has been quiet for MAX_GAP, or its chunk is full.
            created = await index_pending(store, embedder, settle=MAX_GAP)
            if created:
                log.info("Indexed %d new chunks", created)
        except Exception:
            log.exception("Indexing failed, retrying at the next run")
        await asyncio.sleep(interval_seconds)
