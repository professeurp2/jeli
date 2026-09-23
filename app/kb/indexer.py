"""Turn stored messages into embedded chunks."""

import logging
from datetime import datetime, timedelta, timezone

from app.answer.citations import is_ignored
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


async def catch_up_gemini(store: Store, embedder: Embedder, limit: int = 100) -> int:
    """Give Gemini's vector to the passages kept while Google was unreachable, so both memories
    hold the same thing again. Returns how many were filled in; silent when there are none."""
    if not hasattr(store, "chunks_missing_gemini"):
        return 0
    waiting = await store.chunks_missing_gemini(limit)
    if not waiting:
        return 0
    try:
        vectors = await embedder.embed_documents([row["content"] for row in waiting])
    except Exception as error:
        log.warning("Google still unreachable: %d passages still waiting (%s)", len(waiting), error)
        return 0
    for row, vector in zip(waiting, vectors):
        await store.fill_gemini_embedding(row["id"], vector)
    log.info("Caught up %d passages kept while Google was unreachable", len(waiting))
    return len(waiting)


async def index_pending(
    store: Store,
    embedder: Embedder,
    settle: timedelta | None = None,
    ignored: set[str] = frozenset(),
    labels: dict[str, str] | None = None,
) -> int:
    """Chunk and embed every message not indexed yet. Returns the number of chunks created.

    With `settle`, a chat's last chunk is left pending while its conversation may still be
    going on (last message more recent than `settle`), so it is not cut in the middle.
    `ignored`: author keys whose messages are excluded from indexing (e.g. other bots).
    `labels`: readable chat names, written in each conversation chunk's header.
    """
    created = 0
    labels = labels or {}
    if hasattr(store, "refresh_names"):
        await store.refresh_names()  # who each account id is, for the mentions inside messages
    await catch_up_gemini(store, embedder)
    for chat_id in await store.pending_chats():
        header = None
        if chat_id.startswith(RECORDING_PREFIX):
            recording = (await store.recordings([chat_id])).get(chat_id)
            header = recording_header(recording.title, recording.recorded_at) if recording else None
        elif chat_id.startswith(DOCUMENT_PREFIX):
            document = (await store.documents([chat_id])).get(chat_id)
            header = document_header(document.title) if document else None
        messages = await store.pending_messages(chat_id)
        if ignored:
            messages = [m for m in messages if not is_ignored(m, ignored)]
        chunks = chunk_messages(messages, header=header, label=labels.get(chat_id, ""))
        if settle and chunks and chunks[-1].ended_at > datetime.now(timezone.utc) - settle:
            chunks = chunks[:-1]
        for start in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[start : start + BATCH_SIZE]
            texts = [chunk.content for chunk in batch]
            vectors, spares = (
                await embedder.embed_both(texts) if hasattr(embedder, "embed_both")
                else (await embedder.embed_documents(texts), None)
            )
            if vectors is None and not spares:
                log.error("Chat %s: neither memory could embed this batch, kept for the next run", chat_id)
                break
            if vectors is None:
                # Google is unreachable: the passage is kept with the local vector alone, so the
                # memory keeps growing and stays searchable. Gemini's is filled in when it returns.
                log.warning("Chat %s: kept in the spare memory only, waiting for Google", chat_id)
            for position, chunk in enumerate(batch):
                await store.save_chunk(
                    chunk,
                    vectors[position] if vectors else None,
                    MODEL,
                    backup=spares[position] if spares else None,
                )
            created += len(batch)
            log.info("Indexed %d chunks of chat %s", created, chat_id)
    return created
