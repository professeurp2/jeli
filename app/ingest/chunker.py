"""Group chat messages into chunks: the unit that is embedded and retrieved.

A single chat message is usually too short to be found on its own ("yes, Friday"), so
consecutive messages of one conversation are embedded together. A new chunk starts after a
silence, or when the current one is full.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import StoredMessage

MAX_GAP = timedelta(minutes=30)
MAX_CHARS = 1500
# A single message longer than this is cut: the embedding model reads at most 2,048 tokens.
MAX_MESSAGE_CHARS = 4000


@dataclass(frozen=True)
class Chunk:
    chat_id: str
    source: str
    started_at: datetime
    ended_at: datetime
    authors: tuple[str, ...]
    message_ids: tuple[str, ...]
    content: str


def format_message(message: StoredMessage) -> str:
    sent_at = message.sent_at.astimezone(timezone.utc)
    return f"[{sent_at:%Y-%m-%d %H:%M} UTC] {message.author}: {message.text[:MAX_MESSAGE_CHARS]}"


def _make_chunk(messages: list[StoredMessage]) -> Chunk:
    sources = {m.source for m in messages}
    return Chunk(
        chat_id=messages[0].chat_id,
        source=sources.pop() if len(sources) == 1 else "mixed",
        started_at=messages[0].sent_at,
        ended_at=messages[-1].sent_at,
        authors=tuple(dict.fromkeys(m.author for m in messages)),
        message_ids=tuple(m.id for m in messages),
        content="\n".join(format_message(m) for m in messages),
    )


def chunk_messages(
    messages: list[StoredMessage], max_gap: timedelta = MAX_GAP, max_chars: int = MAX_CHARS
) -> list[Chunk]:
    """Chunks of one chat's messages, in time order."""
    chunks: list[Chunk] = []
    current: list[StoredMessage] = []
    size = 0
    for message in sorted(messages, key=lambda m: m.sent_at):
        line = len(format_message(message)) + 1
        if current and (message.sent_at - current[-1].sent_at > max_gap or size + line > max_chars):
            chunks.append(_make_chunk(current))
            current, size = [], 0
        current.append(message)
        size += line
    if current:
        chunks.append(_make_chunk(current))
    return chunks
