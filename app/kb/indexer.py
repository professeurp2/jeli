"""Turn stored messages into embedded chunks."""

import logging
from datetime import datetime, timedelta, timezone

from app.ingest.chunker import chunk_messages
from app.kb.embeddings import BATCH_SIZE, MODEL, Embedder
from app.kb.store import Store

log = logging.getLogger(__name__)

RECORDING_PREFIX = "recording:"
DOCUMENT_PREFIX = "document:"


def recording_header(title: str, recorded_at: datetime) -> str:
    return f"Call recording: {title} ({recorded_at.astimezone(timezone.utc):%d %B %Y})"


def document_header(title: str) -> str:
    return f"Document: {title}"


def document_page(message_sent_at: datetime, shared_at: datetime) -> int:
    """A document's text is stored one part per message, page p at shared_at + p seconds."""
    return max(1, int((message_sent_at - shared_at).total_seconds()))


async def index_pending(store: Store, embedder: Embedder, settle: timedelta | None = None) -> int:
    """Chunk and embed every message not indexed yet. Returns the number of chunks created.

    With `settle`, a chat's last chunk is left pending while its conversation may still be
    going on (last message more recent than `settle`), so it is not cut in the middle.
    """
    created = 0
    for chat_id in await store.pending_chats():
        header = None
        if chat_id.startswith(RECORDING_PREFIX):
            recording = (await store.recordings([chat_id])).get(chat_id)
            header = recording_header(recording.title, recording.recorded_at) if recording else None
        elif chat_id.startswith(DOCUMENT_PREFIX):
            document = (await store.documents([chat_id])).get(chat_id)
            header = document_header(document.title) if document else None
        chunks = chunk_messages(await store.pending_messages(chat_id), header=header)
        if settle and chunks and chunks[-1].ended_at > datetime.now(timezone.utc) - settle:
            chunks = chunks[:-1]
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            vectors = await embedder.embed_documents([chunk.content for chunk in batch])
            for chunk, vector in zip(batch, vectors):
                await store.save_chunk(chunk, vector, MODEL)
            created += len(batch)
            log.info("Indexed %d chunks of chat %s", created, chat_id)
    return created
