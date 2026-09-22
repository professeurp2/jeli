"""Catch-up digest (R8): "what did I miss since Monday?" → highlights, decisions, deadlines, open questions."""

import logging
import time
from datetime import datetime, timezone

from pydantic import BaseModel

from app.answer.citations import POLL_MARK, display_author, ignored_keys, is_ignored, with_tally
from app.answer.language import TEXTS
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import PERSONA
from app.answer.prompts import LANGUAGES
from app.ingest.transcribe import drive_id, is_youtube
from app.kb.store import Store

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 500
MAX_ITEMS = 5
TIMEOUT_SECONDS = 30  # a week of messages is a long read, even for a fast model
CACHE_SECONDS = 600  # when the whole jury asks at once, one summary serves them all

SYSTEM = PERSONA + f"""
Your task now: write the catch-up digest for a member who missed messages.
Each message is timestamped. From the messages below (and the list of call recordings), pick the
most important things a member who missed this SPECIFIC PERIOD needs to know.

Critical rule: report only what is NEW in this period — things announced, decided or scheduled
DURING these messages. Do NOT report past events that members merely mention or reference (e.g.
"the session last week was…" is a reference to the past, not news; skip it unless something new
was said about it). Upcoming deadlines and future events scheduled during this period are news.

Mix announcements (lines marked "(organiser)" first), decisions, upcoming deadlines and unanswered
questions into ONE flat list ordered by importance — no section headers, no categories.

Rules:
- At most {MAX_ITEMS} items, most important first, each under 25 words.
- Lead each item with a *bold* key phrase (WhatsApp syntax: *text*), e.g.:
    "*15h00 CAT* : Open Hour avec @Gift — assister si possible."
    "*Soumission hackathon* (jeu. 24 sept.) : chatbot + code source + notes d'installation."
- Always include the date for deadlines and scheduled future events (inline, not as a prefix).
- Preserve @Name mentions from the source; never include raw phone numbers.
- Skip greetings, thanks, chit-chat and repeated items.
- Say which programme an item concerns when it is not obvious.
- Never include URLs or links.
Use only what the messages say: never add outside knowledge.
"""


class Digest(BaseModel):
    items: list[str]


FRENCH_DAYS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]
FRENCH_MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]

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
        deadlines=None,
    ):
        self.store = store
        self.llm = llm
        self.ignored = ignored_keys(ignored_authors)
        self.chat_labels = chat_labels or {}
        self.organisers: set[str] = set()  # their messages are announcements (set from the settings)
        self._clock = clock
        # R14: the deadlines of the next days close every digest (app.answer.deadlines.Deadlines).
        self.deadlines = deadlines
        self._cache: dict[tuple, tuple[float, str]] = {}

    async def summarize(
        self, since: datetime, language: str, chat_ids: list[str] | None = None, quiet_if_empty: bool = False
    ) -> str | None:
        """All chats by default; `chat_ids` limits the digest, e.g. the daily digest of one group.
        With `quiet_if_empty`, None instead of "nothing new"."""
        # Rounded to 10 minutes: "the last 24 hours" asked a minute apart is the same digest.
        key = (int(since.timestamp()) // 600, language, tuple(chat_ids or ()))
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            digest = cached[1]
        else:
            digest = await self._summarize(since, language, chat_ids)
            self._cache[key] = (self._clock(), digest)
        if digest is None:
            coming_up = await self.deadlines.coming_up_section(language) if self.deadlines else None
            if quiet_if_empty:
                return coming_up  # a quiet day can still bring a reminder
            texts = TEXTS[language]
            latest = await self.store.latest_message_at() if hasattr(self.store, "latest_message_at") else None
            live = await self.store.follows_groups_live() if hasattr(self.store, "follows_groups_live") else False
            if latest and latest < since and live:
                # In the groups, which are simply quiet (a night, a weekend): say so, and since when.
                nothing = texts["catchup_quiet"].format(since=_day(since, language), day=_day(latest, language), time=f"{latest:%H:%M}")
            elif latest and latest < since:
                # Not "nothing new": Jeli simply hasn't seen anything since (not in the groups yet).
                nothing = texts["catchup_not_live"].format(day=_day(latest, language), time=f"{latest:%H:%M}")
            else:
                nothing = texts["catchup_nothing"].format(since=_day(since, language))
            return nothing + (f"\n\n{coming_up}" if coming_up else "")
        return digest

    async def _summarize(self, since: datetime, language: str, chat_ids: list[str] | None) -> str | None:
        texts = TEXTS[language]
        messages = [
            m for m in await self.store.messages_since(since, chat_ids=chat_ids) if not is_ignored(m, self.ignored)
        ]
        recordings = await self.store.recordings_since(since)
        coming_up = await self.deadlines.coming_up_section(language) if self.deadlines else None
        header = texts["catchup_header"].format(since=_day(since, language), messages=len(messages))
        if not messages and not recordings:
            return None

        polls = [m.id for m in messages if m.text.startswith(POLL_MARK)]
        tallies = await self.store.poll_tallies(polls) if polls and hasattr(self.store, "poll_tallies") else {}
        lines = [
            f"[{m.sent_at.astimezone(timezone.utc):%a %d %b %H:%M}] "
            f"{self.chat_labels.get(m.chat_id) or ('the group' if '@' in m.chat_id else m.chat_id)} · "
            f"{display_author(m.author)}{' (organiser)' if is_ignored(m, self.organisers) else ''}: "
            f"{' '.join(with_tally(m.text, tallies.get(m.id)).split())[:MAX_MESSAGE_CHARS]}"
            for m in messages
        ]
        sessions = [f"- «{r.title}» ({_day(r.recorded_at)})" for r in recordings]
        prompt = (
            "Messages, oldest first:\n" + "\n".join(lines)
            + ("\n\nCall recordings available:\n" + "\n".join(sessions) if sessions else "")
            + f"\n\nWrite every item in {LANGUAGES[language]}."
        )
        try:
            digest = await self.llm.generate(prompt, Digest, system=SYSTEM, timeout=TIMEOUT_SECONDS)
        except LLMUnavailable:
            log.error("No model available for the catch-up digest")
            return texts["catchup_unavailable"].format(since=_day(since, language), messages=len(messages))

        parts: list[str] = []
        if digest.items:
            parts.append("\n".join(f"• {item.strip()}" for item in digest.items[:MAX_ITEMS]))
        if recordings:
            rec_lines = [
                f"• {r.title} ({_day(r.recorded_at, language)})"
                + (f"\n  {r.source_url}" if is_youtube(r.source_url or "") or drive_id(r.source_url or "") else "")
                for r in recordings
            ]
            parts.append(texts["catchup_recordings"] + "\n" + "\n".join(rec_lines))
        if coming_up:
            parts.append(coming_up)
        return header + "\n\n" + "\n\n".join(parts)
