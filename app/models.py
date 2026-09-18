from dataclasses import dataclass
from datetime import datetime


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
