"""Following the thread: what a member and Jeli just said to each other, per chat.

After Jeli answers a member, the member's next messages in that chat, for a few minutes, are for
Jeli too when they read like a follow-up (a question, "and for the video?", "send me the file") —
nobody repeats "Jeli" each time in a conversation. Not when they reply to or mention someone else,
and a "thanks" closes the conversation. The recent turns also let Jeli understand the follow-up
("and for the video?" → "what is the deadline for the demo video?").

The turns are kept in memory for speed and written to the database (jeli.conversations), and
read back from it when memory has none: measured (21 Sep), a dozen deployments a day emptied
Jeli's memory of every conversation in progress — members saw a "memory leak".
"""

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.answer.citations import author_key
from app.answer.intents import looks_like_question
from app.models import IncomingMessage

log = logging.getLogger(__name__)

HISTORY_SECONDS = 30 * 60  # turns older than this no longer help understand a message
TURNS_KEPT = 8
FOLLOW_UP_CUE = re.compile(
    r"^\s*(and|also|what about|how about|same for|then|so|but|ok(ay)?[,!.]?\s+(and|so|what|how|when|where|who|can|but)"
    r"|et|aussi|et pour|et si|pareil|alors|donc|mais|ok[,!.]?\s+(et|donc|alors|mais|comment|quand|où))\b",
    re.IGNORECASE,
)
REQUEST = re.compile(
    r"\b(send|share|give|forward|translate|explain|summari[sz]e|tell me|show me|remind me|can you|could you|please"
    r"|envoie|envoyer|partage|donne|traduis|traduire|explique|résume|dis-moi|montre|rappelle|peux-tu|pourrais-tu|stp|svp)\b",
    re.IGNORECASE,
)
CLOSING = re.compile(
    r"^\s*(thanks?( a lot| so much)?|thank you( so much)?|thx|merci( beaucoup| bien)?|ok(ay)?|d'?accord|super|great|cool"
    r"|parfait|perfect|noted|bien reçu|compris|got it|👍|🙏|👌)(\s*,?\s*jeli)?[\s!.🙏👍👌]*$",
    re.IGNORECASE,
)
# A short answer to a question Jeli asked (a number, a choice, a yes): part of the conversation.
SHORT_ANSWER = re.compile(r"^\s*(\d{1,2}|[a-c]|oui|non|yes|no|the (first|second|third|last)|l[ae] (premi[eè]re|deuxi[eè]me|troisi[eè]me|derni[eè]re)|both|les deux)\b[\s!.]*$", re.IGNORECASE)


@dataclass(frozen=True)
class Turn:
    at: float
    message: str
    reply: str
    sources: tuple[str, ...] = field(default_factory=tuple)


def member_of(message: IncomingMessage) -> str:
    return author_key((message.author_id or message.author).split("@")[0])


class Conversations:
    def __init__(self, window_minutes: int = 5, clock: Callable[[], float] = time.monotonic, store=None):
        self.enabled = True
        self.window_minutes = window_minutes
        self.clock = clock
        self.store = store  # keeps the turns across restarts (Store.add_turn / turns), optional
        self._turns: dict[tuple[str, str], deque[Turn]] = defaultdict(lambda: deque(maxlen=TURNS_KEPT))
        self._warmed: set[tuple[str, str]] = set()
        self._pending: set[asyncio.Task] = set()

    def _key(self, message: IncomingMessage) -> tuple[str, str]:
        return message.chat_id, member_of(message)

    async def warm(self, message: IncomingMessage) -> None:
        """Read this member's recent turns from the database once, when memory has none of them
        (after a restart)."""
        key = self._key(message)
        if self.store is None or key in self._warmed:
            return
        self._warmed.add(key)
        if self._turns.get(key):
            return
        try:
            rows = await self.store.turns(key[0], key[1], datetime.now(timezone.utc) - timedelta(seconds=HISTORY_SECONDS))
        except Exception:
            log.exception("Could not read the conversation of a member")
            return
        now_wall, now = datetime.now(timezone.utc), self.clock()
        for row in rows:
            age = (now_wall - row["at"]).total_seconds()
            self._turns[key].append(Turn(now - age, row["message"], row["reply"], tuple(row.get("sources") or ())))

    def note(self, message: IncomingMessage, reply: str, sources=()) -> None:
        """Jeli replied to this member: their next messages may continue the conversation."""
        key = self._key(message)
        self._turns[key].append(Turn(self.clock(), message.text, reply, tuple(sources)))
        self._warmed.add(key)
        if self.store is not None:
            try:
                task = asyncio.get_running_loop().create_task(
                    self.store.add_turn(key[0], key[1], message.is_private, message.text, str(reply), list(sources))
                )
            except RuntimeError:
                return
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def history(self, message: IncomingMessage) -> list[Turn]:
        now = self.clock()
        return [t for t in self._turns.get(self._key(message), ()) if now - t.at <= HISTORY_SECONDS]

    def close(self, message: IncomingMessage) -> None:
        self._turns.pop(self._key(message), None)

    def forget_member(self, member: str) -> None:
        """Every conversation of a member (a team member starting over on the dashboard)."""
        key = author_key(member)
        for chat_member in [k for k in self._turns if k[1] == key]:
            self._turns.pop(chat_member, None)
        if self.store is not None:
            try:
                task = asyncio.get_running_loop().create_task(self.store.forget_turns(key))
                self._pending.add(task)
                task.add_done_callback(self._pending.discard)
            except RuntimeError:
                pass

    def is_open(self, message: IncomingMessage) -> bool:
        """Jeli answered this member in this group a moment ago, and they are not talking to someone
        else: their next message may be for Jeli (a voice note is listened to, to find out)."""
        if not self.enabled or message.is_private or message.addressed_to_bot or message.talks_to_someone_else:
            return False
        turns = self._turns.get(self._key(message))
        return bool(turns) and self.clock() - turns[-1].at <= self.window_minutes * 60

    def is_follow_up(self, message: IncomingMessage) -> bool:
        """A group message not addressed to Jeli that continues a conversation with it."""
        if not self.is_open(message):
            return False
        text = message.text.strip()
        if CLOSING.match(text):
            self.close(message)  # "thanks": the conversation is over, Jeli stays discreet
            return False
        turns = self._turns.get(self._key(message))
        # Jeli asked something back, or listed numbered choices: a short answer is for it.
        asked_back = bool(turns) and ("?" in turns[-1].reply or bool(re.search(r"^\d+\.\s", turns[-1].reply, re.MULTILINE)))
        return (
            looks_like_question(text)
            or bool(FOLLOW_UP_CUE.match(text))
            or bool(REQUEST.search(text))
            or (asked_back and bool(SHORT_ANSWER.match(text)))
        )
