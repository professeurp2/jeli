"""Remember group messages as they arrive (R1, live part)."""

import logging

from app.kb.store import Store
from app.models import IncomingMessage, StoredMessage

log = logging.getLogger(__name__)

SOURCES = {"whatsapp": "whatsapp_live", "telegram": "telegram"}


def to_stored(message: IncomingMessage) -> StoredMessage:
    return StoredMessage(
        id=f"{message.platform}:{message.message_id}",
        chat_id=message.chat_id,
        source=SOURCES[message.platform],
        author=message.author,
        author_id=message.author_id,
        sent_at=message.sent_at,
        text=message.text,
    )


class LiveIngestor:
    """Stores group messages; they are chunked and embedded later by the indexing job.

    Direct messages to Jeli are private conversations: they are never stored.
    """

    def __init__(self, store: Store):
        self.store = store

    async def ingest(self, message: IncomingMessage) -> None:
        if message.is_private or not message.text:
            return
        try:
            await self.store.add_messages([to_stored(message)])
        except Exception:
            log.exception("Could not store message %s", message.message_id)
