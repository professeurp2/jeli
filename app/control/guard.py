"""Spots members who misuse Jeli, and keeps it quiet with them.

Misuse is what a person does to wear Jeli out or turn it against its rules: floods of questions
(the per-member limit trips), the same message again and again, oversized messages, and attempts
to make it ignore its instructions. Each incident is recorded for the dashboard's watchlist, where
the team blocks the member for good or forgives them. Three incidents within an hour and Jeli stays
silent with that member for an hour on its own, without waiting for the team.
"""

import asyncio
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from app.answer.citations import author_key, is_ignored
from app.models import IncomingMessage

log = logging.getLogger(__name__)

INCIDENTS = {
    "flood": "Asked too many questions in a short time",
    "repeat": "Sent the same message again and again",
    "oversized": "Sent very long messages",
    "manipulation": "Tried to make Jeli ignore its rules",
}
MAX_CHARS = 1500  # a real question is far shorter; longer ones stuff the model's input
REPEATS = 3  # the same message this many times…
REPEAT_WINDOW = 600  # …within 10 minutes
COOLDOWN_AFTER = 3  # incidents within an hour…
COOLDOWN_SECONDS = 3600  # …and Jeli stays silent with the member for an hour
MANIPULATION = re.compile(
    r"ignore (?:all |any |the |your )*(?:previous |prior |above |earlier )?(?:instructions|rules|prompts?)"
    r"|(?:reveal|show|print|repeat) (?:me )?(?:your |the )?(?:system )?(?:prompt|instructions)|system prompt"
    r"|jailbreak|developer mode|\bDAN\b|you are no longer|from now on,? you (?:are|will)"
    r"|pretend (?:to be|you are)|act as (?:if|an?) "
    r"|ignore (?:tes|vos) (?:instructions|règles)|oublie (?:toutes )?(?:tes|vos) (?:instructions|règles)"
    r"|fais comme si tu",
    re.IGNORECASE,
)

Record = Callable[[str, str, str], Awaitable[None]]  # (member_key, member_name, kind)


def member_key(message: IncomingMessage) -> str:
    """What identifies a member across names: the digits of their WhatsApp id, else their name."""
    if message.author_id:
        return author_key(message.author_id.split("@")[0])
    return author_key(message.author)


class Guard:
    def __init__(self, record: Record | None = None, clock: Callable[[], float] = time.monotonic):
        self.record = record
        self.clock = clock
        self.blocked: set[str] = set()  # keys of members the team blocked (set from the settings)
        self._incidents: dict[str, deque[float]] = defaultdict(deque)
        self._recent: dict[str, deque[tuple[float, str]]] = defaultdict(deque)
        self._quiet_until: dict[str, float] = {}
        self._pending: set[asyncio.Task] = set()

    def check(self, message: IncomingMessage) -> str | None:
        """Why Jeli should not answer this message ("blocked", "cooling_down", "oversized",
        "repeat"), or None. A manipulation attempt is recorded but still answered: the answer
        stays grounded in the group's messages whatever the question says."""
        if is_ignored(message, self.blocked):
            return "blocked"
        key, now = member_key(message), self.clock()
        if self._quiet_until.get(key, 0) > now:
            return "cooling_down"
        if len(message.text) > MAX_CHARS:
            self.report(message, "oversized")
            return "oversized"
        text = " ".join(message.text.lower().split())
        recent = self._recent[key]
        while recent and now - recent[0][0] > REPEAT_WINDOW:
            recent.popleft()
        recent.append((now, text))
        if sum(1 for _, seen in recent if seen == text) >= REPEATS:
            self.report(message, "repeat")
            return "repeat"
        if MANIPULATION.search(message.text):
            self.report(message, "manipulation")
        return None

    def report(self, message: IncomingMessage, kind: str) -> None:
        key, now = member_key(message), self.clock()
        incidents = self._incidents[key]
        while incidents and now - incidents[0] > COOLDOWN_SECONDS:
            incidents.popleft()
        incidents.append(now)
        log.warning("Misuse by %s: %s", message.author, INCIDENTS[kind])
        if len(incidents) >= COOLDOWN_AFTER and self._quiet_until.get(key, 0) <= now:
            self._quiet_until[key] = now + COOLDOWN_SECONDS
            log.warning("Jeli stays silent with %s for an hour", message.author)
        if self.record:
            task = asyncio.get_running_loop().create_task(self._save(key, message.author, kind))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    async def _save(self, key: str, name: str, kind: str) -> None:
        try:
            await self.record(key, name, kind)
        except Exception:
            log.exception("Could not record an incident")

    def quiet_until(self, key: str) -> float | None:
        """When Jeli will answer this member again (monotonic clock), if it is keeping quiet."""
        until = self._quiet_until.get(key, 0)
        return until if until > self.clock() else None

    def forgive(self, key: str) -> None:
        self._incidents.pop(key, None)
        self._recent.pop(key, None)
        self._quiet_until.pop(key, None)
