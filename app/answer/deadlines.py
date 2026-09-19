"""Deadline reminders (R14): deadlines found in chats and calls, listed on request (/deadlines) and
in the daily digest — never as extra messages of their own, to keep Jeli's profile low.

Extraction reads messages not scanned yet, in batches: the model lists the deadlines a message
states, resolving "Friday" or "tomorrow" from that message's date, and skips those already known.
Every deadline keeps the message that announced it, so it can be cited.
"""

import logging
import re
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel

from app.answer.catchup import _day
from app.answer.citations import display_author, ignored_keys, is_ignored
from app.answer.language import TEXTS
from app.answer.llm import LLM
from app.answer.prompts import PROGRAMMES
from app.kb.indexer import DOCUMENT_PREFIX, RECORDING_PREFIX
from app.kb.store import Store
from app.models import Deadline, StoredMessage

log = logging.getLogger(__name__)

# Measured: with 150 messages per call the model missed the hackathon's own end date; smaller
# batches read each announcement more carefully.
BATCH = 60
# Announcements are the long messages: measured, cutting at 600 characters hid the hackathon's
# "Build Phase: Friday 18 Sept to Thursday 24 Sept", 700 characters into the official announcement.
MAX_MESSAGE_CHARS = 3000
# A deadline more than this far after its message is more likely a misreading than a plan.
MAX_HORIZON = timedelta(days=180)
TIMEOUT_SECONDS = 60

SYSTEM = f"""\
You find deadlines in the messages of a WhatsApp community and in its call transcripts.
{PROGRAMMES}
List the deadlines and dated obligations members must act on: submissions, registrations, forms,
milestones, sessions they must attend. For each:
- what: under 12 words, naming the programme when it helps ("Hackathon: submit the chatbot");
- due_date: YYYY-MM-DD. Resolve relative dates ("tomorrow", "Friday", "next Tuesday") from the
  date of the message that states it. Skip anything without a precise day;
- due_time: the time as stated (e.g. "2:00 PM CAT"), or "";
- programme: "hackathon", "Wadhwani Ignite", "MIT Universal AI", "bootcamp" or "";
- message: the number of the message that states it.
The last day of a period is a deadline for it ("the build phase runs from 18 to 24 September" →
24 September). Skip guesses and questions ("I think it closes Sunday", "is it due Friday?").
When a member contradicts an organiser's announcement about the same date, keep the announcement.
Skip deadlines listed as already known, even if worded differently. Only what a message states:
never invent or guess.
"""


class FoundDeadline(BaseModel):
    what: str
    due_date: str
    due_time: str
    programme: str
    message: int


class FoundDeadlines(BaseModel):
    deadlines: list[FoundDeadline]


def _words(what: str) -> set[str]:
    return {w for w in re.findall(r"\w+", what.lower()) if len(w) > 2 or w.isdigit()} - {"the", "and", "for", "wadhwani", "ignite", "hackathon"}


def same_deadline(a: Deadline, b: Deadline) -> bool:
    """Two wordings of one deadline on the same day ("complete Module 1" / "complete module one
    lesson work"), but never "Module 1" and "Module 2"."""
    if a.due_date != b.due_date:
        return False
    words_a, words_b = _words(a.what), _words(b.what)
    digits_a, digits_b = {w for w in words_a if w.isdigit()}, {w for w in words_b if w.isdigit()}
    if digits_a and digits_b and digits_a != digits_b:
        return False
    smaller = min(len(words_a), len(words_b))
    return bool(smaller) and len(words_a & words_b) / smaller >= 0.6


def _validated(found: FoundDeadlines, messages: list[StoredMessage]) -> list[Deadline]:
    deadlines = []
    for item in found.deadlines:
        if not 1 <= item.message <= len(messages) or not item.what.strip():
            continue
        message = messages[item.message - 1]
        try:
            due = date.fromisoformat(item.due_date.strip())
        except ValueError:
            continue
        said_on = message.sent_at.astimezone(timezone.utc).date()
        if not said_on - timedelta(days=1) <= due <= said_on + MAX_HORIZON:
            continue
        deadlines.append(
            Deadline(
                what=" ".join(item.what.split())[:200],
                due_date=due,
                chat_id=message.chat_id,
                announced_at=message.sent_at,
                due_time=item.due_time.strip()[:40],
                programme=item.programme.strip()[:40],
                message_id=message.id,
                author=message.author,
            )
        )
    return deadlines


class DeadlineExtractor:
    def __init__(self, store: Store, llm: LLM, ignored_authors: list[str] = (), chat_labels: dict[str, str] | None = None):
        self.store = store
        self.llm = llm
        self.ignored = ignored_keys(ignored_authors)
        self.chat_labels = chat_labels or {}

    async def run(self, max_batches: int | None = None) -> int:
        """Scan messages not scanned yet, at most max_batches batches (None: all). Returns the
        number of new deadlines. On a model failure the batch stays unscanned, for the next run."""
        added = batches = 0
        while (max_batches is None or batches < max_batches) and (batch := await self.store.unchecked_messages(limit=BATCH)):
            usable = [m for m in batch if not is_ignored(m, self.ignored) and m.text.strip()]
            if usable:
                added += await self._extract(usable)
            await self.store.mark_deadlines_checked([m.id for m in batch])
            batches += 1
        return added

    async def _extract(self, messages: list[StoredMessage]) -> int:
        first = messages[0].sent_at.astimezone(timezone.utc).date()
        known = await self.store.deadlines_between(first - timedelta(days=1), first + MAX_HORIZON, include_dismissed=True)
        lines = [
            f"[{n}] {m.sent_at.astimezone(timezone.utc):%a %d %b %Y %H:%M} UTC · {self._where(m)} · "
            f"{display_author(m.author)}: {' '.join(m.text.split())[:MAX_MESSAGE_CHARS]}"
            for n, m in enumerate(messages, 1)
        ]
        prompt = (
            "Deadlines already known:\n"
            + ("\n".join(f"- {d.due_date} {d.what}" for d in known) or "- none")
            + "\n\nMessages:\n"
            + "\n".join(lines)
        )
        found = await self.llm.generate(prompt, FoundDeadlines, system=SYSTEM, timeout=TIMEOUT_SECONDS, temperature=0)
        new: list[Deadline] = []
        for deadline in _validated(found, messages):
            if not any(same_deadline(deadline, other) for other in [*known, *new]):
                new.append(deadline)
        return await self.store.add_deadlines(new)

    def _where(self, message: StoredMessage) -> str:
        if message.chat_id.startswith(RECORDING_PREFIX):
            return "call recording"
        if message.chat_id.startswith(DOCUMENT_PREFIX):
            return "shared document"
        return self.chat_labels.get(message.chat_id, message.chat_id)


class Deadlines:
    def __init__(self, store: Store, chat_labels: dict[str, str] | None = None):
        self.store = store
        self.chat_labels = chat_labels or {}

    def _line(self, deadline: Deadline, language: str) -> str:
        when = _day(datetime.combine(deadline.due_date, datetime.min.time(), timezone.utc), language)
        if deadline.due_time:
            when += f", {deadline.due_time}"
        if deadline.chat_id.startswith(RECORDING_PREFIX):
            source = TEXTS[language]["deadline_in_call"]
        elif deadline.chat_id.startswith(DOCUMENT_PREFIX):
            source = TEXTS[language]["deadline_in_document"]
        else:
            source = self.chat_labels.get(deadline.chat_id, deadline.chat_id)
        announced = _day(deadline.announced_at, language)
        return f"• {when} — {deadline.what} ({source}, {display_author(deadline.author)}, {announced})"

    async def upcoming_reply(self, language: str, days: int = 14, today: date | None = None) -> str:
        """/deadlines: what is due in the next two weeks, with who announced it and where."""
        texts = TEXTS[language]
        today = today or datetime.now(timezone.utc).date()
        deadlines = await self.store.deadlines_between(today, today + timedelta(days=days))
        if not deadlines:
            return texts["deadlines_none"].format(days=days)
        return texts["deadlines_header"].format(days=days) + "\n" + "\n".join(self._line(d, language) for d in deadlines)

    async def coming_up_section(self, language: str, days: int = 3, today: date | None = None) -> str | None:
        """For digests: what is due in the next few days, or None."""
        today = today or datetime.now(timezone.utc).date()
        deadlines = await self.store.deadlines_between(today, today + timedelta(days=days))
        if not deadlines:
            return None
        return TEXTS[language]["deadlines_coming_up"] + "\n" + "\n".join(self._line(d, language) for d in deadlines)
