"""Catch-up digest (R8): "what did I miss since Monday?" → highlights, decisions, deadlines, open questions."""

import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel

from app.answer.citations import author_key, display_author
from app.answer.language import TEXTS
from app.answer.llm import LLM, LLMUnavailable
from app.answer.prompts import LANGUAGES, PROGRAMMES
from app.ingest.transcribe import is_youtube
from app.kb.store import Store

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 500
MAX_ITEMS = 5
TIMEOUT_SECONDS = 30  # a week of messages is a long read, even for a fast model
CACHE_SECONDS = 600  # when the whole jury asks at once, one summary serves them all

SYSTEM = f"""\
You write catch-up digests for members of a WhatsApp community who missed messages.
{PROGRAMMES}
From the messages below (and the list of call recordings), extract what a member who missed them
needs to know:
- highlights: announcements and the main discussions;
- decisions: what was decided or confirmed;
- deadlines: deadlines and upcoming dates, always with the date;
- open_questions: questions members asked that nobody answered.
At most {MAX_ITEMS} items per list, most important first, each under 25 words, starting with its
day. Merge repeated items. Skip greetings, thanks and chit-chat. Say which
programme an item is about when it is not obvious. Never include phone numbers or links.
Use only what the messages say: never add outside knowledge.
"""


class Digest(BaseModel):
    highlights: list[str]
    decisions: list[str]
    deadlines: list[str]
    open_questions: list[str]


FRENCH_DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
FRENCH_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
DAY_EXAMPLES = {"en": "Thu 18 Sep", "fr": "jeu. 18 sept."}


def _day(moment: datetime, language: str = "en") -> str:
    moment = moment.astimezone(timezone.utc)
    if language == "fr":
        return f"{FRENCH_DAYS[moment.weekday()]} {moment.day} {FRENCH_MONTHS[moment.month - 1]}"
    return f"{moment:%a %d %b}"


class Catchup:
    def __init__(
        self,
        store: Store,
        llm: LLM,
        ignored_authors: list[str] = (),
        chat_labels: dict[str, str] | None = None,
        clock=time.monotonic,
    ):
        self.store = store
        self.llm = llm
        self.ignored = {author_key(a) for a in ignored_authors}
        self.chat_labels = chat_labels or {}
        self._clock = clock
        self._cache: dict[tuple, tuple[float, str]] = {}

    async def summarize(self, since: datetime, language: str) -> str:
        # Rounded to 10 minutes: "the last 24 hours" asked a minute apart is the same digest.
        key = (int(since.timestamp()) // 600, language)
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            return cached[1]
        digest = await self._summarize(since, language)
        self._cache[key] = (self._clock(), digest)
        return digest

    async def _summarize(self, since: datetime, language: str) -> str:
        texts = TEXTS[language]
        messages = [m for m in await self.store.messages_since(since) if author_key(m.author) not in self.ignored]
        recordings = await self.store.recordings_since(since)
        header = texts["catchup_header"].format(since=_day(since, language), messages=len(messages))
        if not messages and not recordings:
            return texts["catchup_nothing"].format(since=_day(since, language))

        lines = [
            f"[{m.sent_at.astimezone(timezone.utc):%a %d %b %H:%M}] {self.chat_labels.get(m.chat_id, m.chat_id)} · "
            f"{display_author(m.author)}: {' '.join(m.text.split())[:MAX_MESSAGE_CHARS]}"
            for m in messages
        ]
        sessions = [f"- «{r.title}» ({_day(r.recorded_at)})" for r in recordings]
        prompt = (
            "Messages, oldest first:\n" + "\n".join(lines)
            + ("\n\nCall recordings available:\n" + "\n".join(sessions) if sessions else "")
            + f"\n\nWrite every item in {LANGUAGES[language]}, with days written like \"{DAY_EXAMPLES[language]}\"."
        )
        try:
            digest = await self.llm.generate(prompt, Digest, system=SYSTEM, timeout=TIMEOUT_SECONDS)
        except LLMUnavailable:
            log.error("No model available for the catch-up digest")
            return texts["catchup_unavailable"].format(since=_day(since, language), messages=len(messages))

        sections = [
            (texts["catchup_highlights"], digest.highlights),
            (texts["catchup_decisions"], digest.decisions),
            (texts["catchup_deadlines"], digest.deadlines),
            (texts["catchup_questions"], digest.open_questions),
        ]
        body = [f"{title}\n" + "\n".join(f"• {item.strip()}" for item in items[:MAX_ITEMS]) for title, items in sections if items]
        if recordings:
            items = [
                f"• {r.title} ({_day(r.recorded_at, language)})"
                + (f"\n  {r.source_url}" if is_youtube(r.source_url or "") else "")
                for r in recordings
            ]
            body.append(texts["catchup_recordings"] + "\n" + "\n".join(items))
        return header + "\n\n" + "\n\n".join(body)
