"""Group chat messages into chunks: the unit that is embedded and retrieved.

A single chat message is usually too short to be found on its own ("yes, Friday"), so
consecutive messages of one conversation are embedded together. A new chunk starts after a
silence, or when the current one is full.

Each chunk starts with a readable header — where, which day, with whom — and its lines carry
only the time of day. Measured (21 Sep): the raw "[2026-09-12 14:05 UTC]" prefix on every line
was noise in the embedding, while the day and the group were nowhere in the text a question
could match.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.models import StoredMessage

MAX_GAP = timedelta(minutes=30)
MAX_CHARS = 1500
# A single message longer than this is cut: the embedding model reads at most 2,048 tokens.
MAX_MESSAGE_CHARS = 4000
MAX_HEADER_AUTHORS = 6


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
    return f"[{sent_at:%H:%M}] {message.author}: {message.text[:MAX_MESSAGE_CHARS]}"


def conversation_header(label: str, started_at: datetime, ended_at: datetime, authors: tuple[str, ...]) -> str:
    """"Conversation in METI cohort, Thursday 17 September 2026, 14:05–14:40 UTC, with Awa, Moussa." """
    start, end = started_at.astimezone(timezone.utc), ended_at.astimezone(timezone.utc)
    where = label.strip() or "the group"
    when = f"{start:%A %d %B %Y}, {start:%H:%M}" + (f"–{end:%H:%M}" if end != start else "") + " UTC"
    names = [a for a in authors if a.strip()][:MAX_HEADER_AUTHORS]
    who = f", with {', '.join(names)}" if names else ""
    return f"Conversation in {where}, {when}{who}."


def _make_chunk(messages: list[StoredMessage], header: str | None, label: str) -> Chunk:
    sources = {m.source for m in messages}
    lines = [format_message(m) for m in messages]
    authors = tuple(dict.fromkeys(m.author for m in messages))
    started_at, ended_at = messages[0].sent_at, messages[-1].sent_at
    top = header if header is not None else conversation_header(label, started_at, ended_at, authors)
    return Chunk(
        chat_id=messages[0].chat_id,
        source=sources.pop() if len(sources) == 1 else "mixed",
        started_at=started_at,
        ended_at=ended_at,
        authors=authors,
        message_ids=tuple(m.id for m in messages),
        content="\n".join([top, *lines] if top else lines),
    )


def chunk_messages(
    messages: list[StoredMessage],
    max_gap: timedelta = MAX_GAP,
    max_chars: int = MAX_CHARS,
    header: str | None = None,
    label: str = "",
) -> list[Chunk]:
    """Chunks of one chat's messages, in time order.

    `header` starts every chunk, e.g. a recording's title, so that "what was said in the Module 1
    session?" finds that session's chunks. Without one, each chunk gets a conversation header
    naming the chat (`label`), the day and the people talking.
    """
    chunks: list[Chunk] = []
    current: list[StoredMessage] = []
    header_size = len(header) + 1 if header else 90  # a conversation header is about this long
    size = header_size
    for message in sorted(messages, key=lambda m: m.sent_at):
        line = len(format_message(message)) + 1
        if current and (message.sent_at - current[-1].sent_at > max_gap or size + line > max_chars):
            chunks.append(_make_chunk(current, header, label))
            current, size = [], header_size
        current.append(message)
        size += line
    if current:
        chunks.append(_make_chunk(current, header, label))
    return chunks
