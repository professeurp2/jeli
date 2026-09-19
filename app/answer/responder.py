"""What Jeli replies to a message. Every adapter calls Responder.respond with every message."""

import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.adapters.pacing import SlidingWindowLimiter
from app.answer.catchup import Catchup
from app.answer.conversation import Conversations
from app.answer.deadlines import Deadlines
from app.answer.intents import (
    SESSION_WORD,
    catchup_since,
    is_deadlines_request,
    is_recap_request,
    looks_like_question,
    parse_since,
)
from app.answer.language import TEXTS, detect_language
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.answer.understand import Understander
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
    if getattr(reply, "unanswered", False) or reply == texts["dont_know"] or reply.startswith(texts["dont_know_near"]):
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
        understander: Understander | None = None,
        conversations: Conversations | None = None,
        documents=None,
    ):
        # Sends the documents Jeli keeps, translated if asked (app/answer/documents.py).
        self.documents = documents
        # Reads each message with the conversation so far (small talk, follow-ups, search queries).
        self.understander = understander or Understander(None)
        # What each member and Jeli just said to each other, to follow the thread.
        self.conversations = conversations or Conversations()
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
            reply, kind = await self._route(message, language)
            if reply:
                self.conversations.note(message, reply)
        else:
            reply, kind = await self._already_answered(message), "already_answered"
        if reply and self.record:
            event = UsageEvent(
                kind=kind,
                outcome=_outcome(kind, reply, language),
                language=language,
                is_private=message.is_private,
                latency_ms=int((time.monotonic() - started) * 1000),
                question="" if message.is_private else shareable_question(message.text),
                channel=message.platform,
                chat_id="" if message.is_private else message.chat_id,
            )
            try:
                await self.record(event)
            except Exception:
                log.exception("Could not record a usage event")  # counting must never cost a reply
        return reply

    def is_follow_up(self, message: IncomingMessage) -> bool:
        """A group message not addressed to Jeli, but continuing a conversation with it."""
        return self.conversations.is_follow_up(message)

    def in_conversation(self, message: IncomingMessage) -> bool:
        """This member was talking with Jeli a moment ago (whatever their new message says)."""
        return self.conversations.is_open(message)

    async def _route(self, message: IncomingMessage, language: str) -> tuple[str, str]:
        texts = TEXTS[language]
        text = message.text.strip()
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
        understood = await self.understander.understand(text, language, self.conversations.history(message))
        if understood.kind in ("social", "about_jeli"):
            return understood.reply, "social"
        if understood.kind == "file" and self.documents:
            reply = await self.documents.reply(text, language, self.conversations.history(message))
            if reply is not None:
                return reply, "file"
        if understood.kind == "catchup" and self.catchup and not SESSION_WORD.search(text):
            return await self.catchup.summarize(parse_since(text, datetime.now(timezone.utc)), language), "catchup"
        question = understood.standalone
        if question != text and is_deadlines_request(question) and self.deadlines:
            return await self.deadlines.upcoming_reply(language), "deadlines"  # "and the deadlines?"
        if self.answerer is None:
            return texts["not_ready"], "question"
        if self.recaps:
            # "What questions were asked during the MIT call?": the whole call is read, not passages.
            from_session = await self.recaps.answer(question, language)
            if from_session:
                return from_session, "question"
        answer = await self.answerer.answer(
            question, asker=message.author, chat_id=message.chat_id, asker_id=message.author_id, queries=understood.queries
        )
        return answer, "question"

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
        reply = await self.answerer.already_answered(
            message.text, self.duplicate_min_similarity, chat_id=message.chat_id, asker_id=message.author_id
        )
        if reply and not self.uninvited.allow(message.chat_id):
            log.info("Already-answered question in %s, but the hourly cap on uninvited replies is reached", message.chat_id)
            return None
        return reply
