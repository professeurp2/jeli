from collections.abc import Awaitable, Callable
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
    # Replies to, or mentions, another member: this message is not for Jeli, even in a conversation.
    talks_to_someone_else: bool = False
    # A voice note: where the channel keeps its audio (its text is empty until Jeli listens to it).
    voice_url: str | None = None
    voice_mimetype: str = ""
    # Asked by voice: Jeli answers by voice too.
    reply_by_voice: bool = False
    # Body of the WhatsApp message the member is quoting, when they typed a real question alongside.
    # Included as context so the LLM knows what "ça" / "this" / "ce message" refers to.
    quoted_context: str = ""
    # An image or photo the member shared (a screenshot, a table, a chart …).
    # Jeli downloads it and asks Gemini to describe it, then prepends the description to the text.
    image_url: str | None = None
    image_mimetype: str = ""


@dataclass(frozen=True)
class Attachment:
    """A file Jeli sends: a document it keeps, or one it made (a translation)."""

    filename: str
    mimetype: str
    data: bytes
    caption: str = ""
    document_id: str = ""  # the document Jeli keeps it as, if any


class Reply(str):
    """Jeli's reply as WhatsApp shows it: the text, and

    - reply_to: the message it answers, quoted above it — by default the member's question, or the
      source message itself, WhatsApp's own way to point at what was said (quoted: its author and
      text, for previews);
    - mentions: members @mentioned in the text (as "@<number>");
    - a file: at once (attachment), or once it is made (pending, e.g. a translation) — the channel
      sends the text first and the file when ready, or the text pending returns instead."""

    reply_to: str | None
    quoted: tuple[str, str] | None
    mentions: list[str]
    attachment: Attachment | None
    pending: Callable[[], Awaitable[Attachment | str]] | None

    unanswered: bool  # explains why there is no answer (counted as "couldn't answer")

    def __new__(cls, text: str, *, reply_to=None, quoted=None, mentions=(), attachment=None, pending=None, unanswered=False):
        reply = super().__new__(cls, text)
        reply.reply_to, reply.quoted, reply.mentions = reply_to, quoted, list(mentions)
        reply.attachment, reply.pending, reply.unanswered = attachment, pending, unanswered
        return reply


@dataclass(frozen=True)
class Document:
    """A document Jeli keeps (PDF, Word, text); its text is StoredMessages with chat_id = id."""

    id: str
    title: str
    filename: str
    mimetype: str
    size_bytes: int
    pages: int
    language: str
    shared_by: str
    shared_at: datetime
    chat_id: str = ""
    translation_of: str | None = None


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
    """One interaction, counted for the dashboard (R13). Never the author; the text only for a
    question asked in a group, which every member there has seen (phone numbers masked)."""

    kind: str  # question, catchup, recap, deadlines, search, already_answered, help
    outcome: str = ""  # for questions: answered, dont_know, sources_only, not_ready
    language: str = ""
    is_private: bool = False
    latency_ms: int | None = None
    question: str = ""  # the message, for group messages and tries on the dashboard (never private ones)
    channel: str = ""  # whatsapp, telegram, dashboard (a try by the team)
    chat_id: str = ""  # the group; "" for private messages


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
    id: int | None = None  # set once stored


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
