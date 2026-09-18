"""What Jeli replies to a message addressed to it. Every adapter calls Responder.respond."""

import logging

from app.answer.language import TEXTS, detect_language
from app.answer.rag import Answerer
from app.models import IncomingMessage

log = logging.getLogger(__name__)

HELP_COMMANDS = {"/start", "/help", "/aide", "help", "aide"}


class Responder:
    def __init__(self, answerer: Answerer | None = None):
        self.answerer = answerer

    async def respond(self, message: IncomingMessage) -> str | None:
        """The reply to a message, or None to stay silent."""
        if not message.addressed_to_bot:
            return None
        text = message.text.strip()
        texts = TEXTS[detect_language(text)]
        if not text or text.lower() in HELP_COMMANDS:
            return texts["help"]
        if text.startswith("/"):
            # /catchup and /search arrive with the next features; until then, explain what works.
            return texts["help"]
        if self.answerer is None:
            return texts["not_ready"]
        return await self.answerer.answer(text, asker=message.author)
