"""What Jeli replies to a message. Every adapter calls Responder.respond with every message."""

import logging
import re

from app.adapters.pacing import SlidingWindowLimiter
from app.answer.catchup import Catchup
from app.answer.intents import catchup_since, is_recap_request, looks_like_question
from app.answer.language import TEXTS, detect_language
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.models import IncomingMessage

log = logging.getLogger(__name__)

HELP_COMMANDS = {"/start", "/help", "/aide", "help", "aide"}
SEARCH_COMMAND = re.compile(r"^/(?:search|cherche|chercher)\b(.*)$", re.IGNORECASE | re.DOTALL)


class Responder:
    def __init__(
        self,
        answerer: Answerer | None = None,
        catchup: Catchup | None = None,
        duplicate_detection: bool = False,
        duplicate_min_similarity: float = 0.70,
        duplicate_replies_per_hour: int = 3,
        recaps: Recaps | None = None,
    ):
        self.answerer = answerer
        self.catchup = catchup
        self.recaps = recaps
        self.duplicate_detection = duplicate_detection
        self.duplicate_min_similarity = duplicate_min_similarity
        # Uninvited replies are capped per group, on top of the channel's own anti-ban limits.
        self.uninvited = SlidingWindowLimiter(duplicate_replies_per_hour, 3600)

    async def respond(self, message: IncomingMessage) -> str | None:
        """The reply to a message, or None to stay silent."""
        if not message.addressed_to_bot:
            return await self._already_answered(message)

        text = message.text.strip()
        language = detect_language(text)
        texts = TEXTS[language]
        if not text or text.lower() in HELP_COMMANDS:
            return texts["help"]
        since = catchup_since(text)
        if since is not None:
            return await self.catchup.summarize(since, language) if self.catchup else texts["not_ready"]
        if is_recap_request(text):
            if self.recaps is None:
                return texts["not_ready"]
            recap = await self.recaps.reply(text, language)
            if recap:
                return recap  # otherwise it is not about a recorded session: a normal question
        search = SEARCH_COMMAND.match(text)
        if search and search.group(1).strip():
            return await self.answerer.where_discussed(search.group(1).strip()) if self.answerer else texts["not_ready"]
        if text.startswith("/"):
            return texts["help"]
        if self.answerer is None:
            return texts["not_ready"]
        return await self.answerer.answer(text, asker=message.author)

    async def _already_answered(self, message: IncomingMessage) -> str | None:
        """R7: a question asked in the group that the group already answered gets a pointer to it."""
        if (
            not self.duplicate_detection
            or self.answerer is None
            or message.is_private
            or not looks_like_question(message.text)
            or self.answerer.is_ignored(message)
        ):
            return None
        reply = await self.answerer.already_answered(message.text, self.duplicate_min_similarity)
        if reply and not self.uninvited.allow(message.chat_id):
            log.info("Already-answered question in %s, but the hourly cap on uninvited replies is reached", message.chat_id)
            return None
        return reply
