"""What Jeli replies to a message. Every adapter calls Responder.respond with every message."""

import logging
import re
import time
from collections.abc import Awaitable, Callable

from app.adapters.pacing import SlidingWindowLimiter
from app.answer.catchup import Catchup
from app.answer.deadlines import Deadlines
from app.answer.intents import catchup_since, is_deadlines_request, is_recap_request, looks_like_question
from app.answer.language import TEXTS, detect_language
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.models import IncomingMessage, UsageEvent

log = logging.getLogger(__name__)

HELP_COMMANDS = {"/start", "/help", "/aide", "help", "aide"}
SEARCH_COMMAND = re.compile(r"^/(?:search|cherche|chercher)\b(.*)$", re.IGNORECASE | re.DOTALL)
MENTION = re.compile(r"@\d{6,}")  # WhatsApp mentions carry the member's number
NUMBER = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def shareable_question(text: str) -> str:
    """A group question as the team sees it on the dashboard: no mention, no phone number."""
    text = MENTION.sub("", text)
    text = NUMBER.sub(lambda m: "···" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)
    return " ".join(text.split())[:300]


def _outcome(kind: str, reply: str, language: str) -> str:
    """How a question went, from its reply: answered, dont_know, sources_only or not_ready."""
    if kind != "question":
        return ""
    texts = TEXTS[language]
    if reply == texts["not_ready"]:
        return "not_ready"
    if reply == texts["dont_know"]:
        return "dont_know"
    if reply.startswith(texts["fallback"]):
        return "sources_only"
    return "answered"


class Responder:
    def __init__(
        self,
        answerer: Answerer | None = None,
        catchup: Catchup | None = None,
        duplicate_detection: bool = False,
        duplicate_min_similarity: float = 0.70,
        duplicate_replies_per_hour: int = 3,
        recaps: Recaps | None = None,
        deadlines: Deadlines | None = None,
        record: Callable[[UsageEvent], Awaitable[None]] | None = None,
    ):
        # Usage counters for the dashboard (Store.record_event); never authors, and no text
        # but the questions asked in groups.
        self.record = record
        self.answerer = answerer
        self.catchup = catchup
        self.recaps = recaps
        self.deadlines = deadlines
        self.duplicate_detection = duplicate_detection
        self.duplicate_min_similarity = duplicate_min_similarity
        # Uninvited replies are capped per group, on top of the channel's own anti-ban limits.
        self.uninvited = SlidingWindowLimiter(duplicate_replies_per_hour, 3600)

    async def respond(self, message: IncomingMessage) -> str | None:
        """The reply to a message, or None to stay silent. Each reply is counted for the dashboard."""
        started = time.monotonic()
        language = detect_language(message.text)
        if message.addressed_to_bot:
            reply, kind = await self._route(message.text.strip(), message.author, language)
        else:
            reply, kind = await self._already_answered(message), "already_answered"
        if reply and self.record and message.platform != "dashboard":  # tries from the dashboard are not usage
            event = UsageEvent(
                kind=kind,
                outcome=_outcome(kind, reply, language),
                language=language,
                is_private=message.is_private,
                latency_ms=int((time.monotonic() - started) * 1000),
                question=shareable_question(message.text) if kind == "question" and not message.is_private else "",
            )
            try:
                await self.record(event)
            except Exception:
                log.exception("Could not record a usage event")  # counting must never cost a reply
        return reply

    async def _route(self, text: str, asker: str, language: str) -> tuple[str, str]:
        texts = TEXTS[language]
        if not text or text.lower() in HELP_COMMANDS:
            return texts["help"], "help"
        since = catchup_since(text)
        if since is not None:
            return (await self.catchup.summarize(since, language) if self.catchup else texts["not_ready"]), "catchup"
        if is_deadlines_request(text):
            return (await self.deadlines.upcoming_reply(language) if self.deadlines else texts["not_ready"]), "deadlines"
        if is_recap_request(text):
            if self.recaps is None:
                return texts["not_ready"], "recap"
            recap = await self.recaps.reply(text, language)
            if recap:
                return recap, "recap"  # otherwise it is not about a recorded session: a normal question
        search = SEARCH_COMMAND.match(text)
        if search and search.group(1).strip():
            topic = search.group(1).strip()
            return (await self.answerer.where_discussed(topic) if self.answerer else texts["not_ready"]), "search"
        if text.startswith("/"):
            return texts["help"], "help"
        if self.answerer is None:
            return texts["not_ready"], "question"
        return await self.answerer.answer(text, asker=asker), "question"

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
