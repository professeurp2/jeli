from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class IncomingMessage:
    """A chat message normalised by an adapter, so the core never sees platform-specific types."""

    platform: str  # "telegram", "whatsapp", ...
    chat_id: str
    message_id: str
    author: str
    text: str  # with the bot mention stripped
    sent_at: datetime
    is_private: bool
    # True for a direct message, an @mention of the bot, or a reply to one of its messages.
    addressed_to_bot: bool
    link: str | None = None
    # Stable platform id of the author (rate limits, and later storage); the display name can change.
    author_id: str | None = None


@dataclass(frozen=True)
class Recording:
    """A call recording; its transcript segments are StoredMessages with chat_id = id."""

    id: str
    title: str
    recorded_at: datetime
    method: str  # "gemini" or "subtitles"
    source_url: str | None = None
    duration_seconds: int | None = None
    recap: dict | None = None  # per language, see app/answer/recaps.py


@dataclass(frozen=True)
class UsageEvent:
    """One interaction, counted for the dashboard (R13): never the text, never the author."""

    kind: str  # question, catchup, recap, deadlines, search, already_answered, help
    outcome: str = ""  # for questions: answered, dont_know, sources_only, not_ready
    language: str = ""
    is_private: bool = False
    latency_ms: int | None = None


@dataclass(frozen=True)
class Deadline:
    """A deadline found in a chat or a call (R14), linked to the message that announced it."""

    what: str
    due_date: date
    chat_id: str
    announced_at: datetime
    due_time: str = ""
    programme: str = ""
    message_id: str | None = None
    author: str = ""


@dataclass(frozen=True)
class StoredMessage:
    """A message as kept in the knowledge base, whatever its origin."""

    id: str
    chat_id: str
    source: str  # "whatsapp_export", "whatsapp_live" or "telegram"
    author: str
    sent_at: datetime
    text: str
    author_id: str | None = None
