"""What Jeli replies to a message. Every adapter calls Responder.respond with every message.

The responder is thin: slash commands and a few everyday messages are answered at once; every
other message goes through the understanding step (understand.py), which says what the member
wants, and the responder hands it to the right part of Jeli — catch-up, session recap or
question, deadlines, documents, listings, the previous answer's sources, or a grounded answer.
The search on the raw message starts in parallel with the understanding, so that the two model
calls cost the time of one.
"""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.adapters.pacing import SlidingWindowLimiter
from app.answer.catchup import Catchup
from app.answer.conversation import Conversations, member_of
from app.answer.deadlines import Deadlines
from app.answer.intents import SESSION_WORD, catchup_since, is_deadlines_request, is_recap_request, looks_like_question, parse_since
from app.answer.language import TEXTS, detect_language
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.answer.understand import Understander, parse_since as parse_iso
from app.models import IncomingMessage, Reply, UsageEvent

log = logging.getLogger(__name__)

HELP_COMMANDS = {"/start", "/help", "/aide", "help", "aide"}
SEARCH_COMMAND = re.compile(r"^/(?:search|cherche|chercher)\b(.*)$", re.IGNORECASE | re.DOTALL)
MENTION = re.compile(r"@\d{6,}")  # WhatsApp mentions carry the member's number
NUMBER = re.compile(r"\+?\d[\d\s().-]{7,}\d")
# Bare number follow-up after /recap listing: "4", "#4", "session 4", "4."
BARE_SESSION_NUMBER = re.compile(r"^(?:session\s+)?#?(\d+)\.?$", re.IGNORECASE)
INTRO = re.compile(
    r"\b(je\s+me\s+pr[eé]sente|je\s+m.appelle|je\s+suis\s+nouveau|my\s+name\s+is|i\s+(am|'m)\s+new|just\s+joined|i\s+work\s+on|je\s+travaille\s+sur)\b",
    re.IGNORECASE,
)


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
        store=None,
    ):
        # Sends the documents Jeli keeps, translated if asked (app/answer/documents.py).
        self.documents = documents
        # Reads each message with the conversation so far (small talk, follow-ups, search queries).
        self.understander = understander or Understander(None)
        # What each member and Jeli just said to each other, to follow the thread.
        self.conversations = conversations or Conversations(store=store)
        # Usage counters for the dashboard (Store.record_event); never authors, and no text
        # but the questions asked in groups.
        self.record = record
        # What Jeli knows of each member (name, language, their own introduction), optional.
        self.store = store
        self.answerer = answerer
        self.catchup = catchup
        self.recaps = recaps
        self.deadlines = deadlines
        self.duplicate_detection = duplicate_detection
        self.duplicate_min_similarity = duplicate_min_similarity
        # Uninvited replies are capped per group, on top of the channel's own anti-ban limits.
        self.uninvited = SlidingWindowLimiter(duplicate_replies_per_hour, 3600)
        self._pending: set[asyncio.Task] = set()
        # Reminders members ask for (app/answer/reminders.py), set at startup.
        self.reminders = None

    async def respond(self, message: IncomingMessage) -> str | None:
        """The reply to a message, or None to stay silent. Each reply is counted for the dashboard."""
        started = time.monotonic()
        language = detect_language(message.text)
        if message.addressed_to_bot:
            await self.conversations.warm(message)
            reply, kind, language = await self._route(message, language)
            if reply:
                # The language the understanding step chose goes with the reply (for its voice note).
                if not isinstance(reply, Reply):
                    reply = Reply(reply)
                reply.language = language
                self.conversations.note(message, reply, getattr(reply, "cited", ()))
                self._remember(message, language)
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

    async def warm(self, message: IncomingMessage) -> None:
        """Read the member's recent conversation back after a restart (channels call it first)."""
        await self.conversations.warm(message)

    # --- What Jeli knows of the member ------------------------------------------------------------

    async def _member(self, message: IncomingMessage) -> str:
        """A line about the member for the prompts: their name, language, own introduction."""
        parts = []
        if message.author and message.author != "Someone":
            parts.append(f"Name shown on WhatsApp: {message.author}.")
        if self.store is not None and hasattr(self.store, "member"):
            try:
                profile = await self.store.member(member_of(message))
            except Exception:
                log.exception("Could not read a member's profile")
                profile = None
            if profile:
                if profile.get("language"):
                    parts.append(f"Usually writes in {profile['language']}.")
                notes = profile.get("notes") or {}
                if notes.get("intro"):
                    parts.append(f"Introduced themselves as: {notes['intro'][:300]}")
                if notes.get("last_topic"):
                    parts.append(f"Last asked about: {notes['last_topic'][:120]}")
        return " ".join(parts)

    def _remember(self, message: IncomingMessage, language: str, topic: str = "") -> None:
        if self.store is None or not hasattr(self.store, "remember_member"):
            return
        notes = {}
        if topic:
            notes["last_topic"] = topic[:120]
        if INTRO.search(message.text):
            notes["intro"] = " ".join(message.text.split())[:300]
        try:
            task = asyncio.get_running_loop().create_task(
                self.store.remember_member(member_of(message), name=message.author, language=language, notes=notes)
            )
        except RuntimeError:
            return
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # --- Routing ----------------------------------------------------------------------------------

    async def _route(self, message: IncomingMessage, language: str) -> tuple[str, str, str]:
        texts = TEXTS[language]
        text = message.text.strip()
        # When the member quotes an older message alongside their question, prepend it as context
        # so the model understands what "ça" / "ce message" / "this" refers to.
        text_with_context = f"[Message cité: {message.quoted_context}]\n{text}" if message.quoted_context else text
        if not text or text.lower() in HELP_COMMANDS:
            return texts["help"], "help", language
        if text.startswith("/"):
            return (*await self._command(text, language), language)
        # Bare number after Jeli listed the sessions: "4", "session 4".
        if self.recaps and self.in_conversation_or_open(message):
            bare = BARE_SESSION_NUMBER.match(text)
            if bare:
                recap = await self.recaps.reply(f"/recap {bare.group(1)}", language)
                if recap:
                    return recap, "recap", language

        turns = self.conversations.history(message)
        member = await self._member(message)
        prefetch = asyncio.ensure_future(self.answerer.prefetch(text)) if self.answerer is not None else None
        understood = await self.understander.understand(text_with_context, language, turns, member=member)
        language = understood.language or language
        texts = TEXTS[language]
        kind, question = understood.kind, understood.standalone or text
        self._remember(message, language, topic=question if kind in ("question", "session_question") else "")

        if kind in ("social", "about_jeli", "clarify"):
            self._drop(prefetch)
            return understood.reply or texts["greeting_reply"], "social" if kind != "clarify" else "clarify", language
        if kind == "sources":
            self._drop(prefetch)
            cited = turns[-1].sources if turns else ()
            return ("\n\n".join(cited) if cited else texts["no_sources"]), "sources", language
        if kind == "voice":
            self._drop(prefetch)
            last = turns[-1].reply if turns else ""
            return (Reply(last) if last else texts["help"]), "voice", language
        if kind == "reminder" and self.reminders is not None:
            self._drop(prefetch)
            return await self.reminders.handle(message, question, language, turns), "reminder", language
        if kind == "catchup" and self.catchup:
            self._drop(prefetch)
            since = parse_iso(understood.since) or parse_since(text, datetime.now(timezone.utc))
            return await self.catchup.summarize(since, language), "catchup", language
        if kind == "deadlines" and self.deadlines:
            self._drop(prefetch)
            return await self.deadlines.upcoming_reply(language), "deadlines", language
        if kind == "list_documents":
            self._drop(prefetch)
            return await self._list_documents(language), "list", language
        if kind == "list_sessions" and self.recaps:
            self._drop(prefetch)
            return (await self.recaps.reply("/recap", language)) or texts["not_ready"], "list", language
        if kind == "recap" and self.recaps:
            recap = await self.recaps.reply(f"{understood.session} {text}".strip(), language)
            if recap:
                self._drop(prefetch)
                return recap, "recap", language
            kind = "question"
        if kind == "file" and self.documents:
            reply = await self.documents.reply(text_with_context, language, turns)
            if reply is not None:
                self._drop(prefetch)
                return reply, "file", language
            kind = "question"
        if self.answerer is None:
            self._drop(prefetch)
            return texts["not_ready"], "question", language
        if self.recaps and (kind == "session_question" or SESSION_WORD.search(question)):
            # "What questions were asked during the MIT call?": the whole call is read, not passages.
            from_session = await self.recaps.answer(f"{understood.session} {question}".strip() if understood.session else question, language)
            if from_session:
                self._drop(prefetch)
                return from_session, "question", language
        prefetched = await prefetch if prefetch is not None else None
        answer = await self.answerer.answer(
            question,
            asker=message.author,
            chat_id=message.chat_id,
            asker_id=message.author_id,
            queries=understood.queries,
            language=language,
            member=member,
            prefetched=prefetched,
        )
        return answer, "question", language

    def in_conversation_or_open(self, message: IncomingMessage) -> bool:
        return bool(self.conversations.history(message))

    @staticmethod
    def _drop(task) -> None:
        if task is not None and not task.done():
            task.cancel()

    async def _command(self, text: str, language: str) -> tuple[str, str]:
        texts = TEXTS[language]
        since = catchup_since(text)
        if since is not None:
            return (await self.catchup.summarize(since, language) if self.catchup else texts["not_ready"]), "catchup"
        if is_deadlines_request(text):
            return (await self.deadlines.upcoming_reply(language) if self.deadlines else texts["not_ready"]), "deadlines"
        if is_recap_request(text):
            if self.recaps is None:
                return texts["not_ready"], "recap"
            recap = await self.recaps.reply(text, language)
            return (recap or texts["not_ready"]), "recap"
        search = SEARCH_COMMAND.match(text)
        if search and search.group(1).strip():
            topic = search.group(1).strip()
            return (await self.answerer.where_discussed(topic) if self.answerer else texts["not_ready"]), "search"
        return texts["help"], "help"

    async def _list_documents(self, language: str) -> str:
        texts = TEXTS[language]
        store = getattr(self.documents, "store", None) or self.store
        if store is None or not hasattr(store, "list_documents"):
            return texts["not_ready"]
        documents = await store.list_documents()
        if not documents:
            return texts["documents_none"]
        lines = [f"• *{d.title}* ({d.pages} p.)" for d in documents[:15]]
        return texts["documents_header"].format(n=len(documents)) + "\n" + "\n".join(lines)

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
