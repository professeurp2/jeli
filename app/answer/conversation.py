"""Following the thread: what a member and Jeli just said to each other, per chat.

After Jeli answers a member, the member's next messages in that chat, for a few minutes, are for
Jeli too when they read like a follow-up (a question, "and for the video?", "send me the file") —
nobody repeats "Jeli" each time in a conversation. Not when they reply to or mention someone else,
and a "thanks" closes the conversation. The recent turns also let Jeli understand the follow-up
("and for the video?" → "what is the deadline for the demo video?").
"""

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass

from app.answer.citations import author_key
from app.answer.intents import looks_like_question
from app.models import IncomingMessage

HISTORY_SECONDS = 20 * 60  # turns older than this no longer help understand a message
TURNS_KEPT = 4
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
    r"|parfait|perfect|noted|bien reçu|compris|got it|👍|🙏|👌)[\s!.🙏👍👌]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Turn:
    at: float
    message: str
    reply: str


def member_of(message: IncomingMessage) -> str:
    return author_key((message.author_id or message.author).split("@")[0])


class Conversations:
    def __init__(self, window_minutes: int = 5, clock: Callable[[], float] = time.monotonic):
        self.enabled = True
        self.window_minutes = window_minutes
        self.clock = clock
        self._turns: dict[tuple[str, str], deque[Turn]] = defaultdict(lambda: deque(maxlen=TURNS_KEPT))

    def _key(self, message: IncomingMessage) -> tuple[str, str]:
        return message.chat_id, member_of(message)

    def note(self, message: IncomingMessage, reply: str) -> None:
        """Jeli replied to this member: their next messages may continue the conversation."""
        self._turns[self._key(message)].append(Turn(self.clock(), message.text, reply))

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
        return looks_like_question(text) or bool(FOLLOW_UP_CUE.match(text)) or bool(REQUEST.search(text))
