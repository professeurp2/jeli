"""Session recaps (R9): for each call recording, a summary, decisions, action items with owners,
and key moments linked to the exact second of the video.

A recap is generated once from the whole transcript (20–30k tokens, one model call) and stored per
language in jeli.recordings.recap: English at import, other languages the first time they are asked.
"""

import logging
import re
import unicodedata
from datetime import timedelta, timezone

from pydantic import BaseModel

from app.answer.citations import recording_quote, timestamped_link
from app.answer.intents import SESSION_WORD
from app.answer.language import TEXTS
from app.answer.llm import LLM, LLMUnavailable
from app.answer.prompts import LANGUAGES, PROGRAMMES
from app.ingest.transcribe import format_offset, is_youtube, parse_timestamp
from app.kb.store import Store
from app.models import Recording, Reply

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 120
SESSION_QUESTION_TIMEOUT = 25  # a whole call's transcript is read (about 20k tokens for an hour)
MAX_KEY_MOMENTS = 6

SYSTEM = f"""\
You write the recap of a recorded call for members of a WhatsApp community who missed it.
{PROGRAMMES}
From the timestamped transcript, write:
- summary: the 3 to 6 main points, one sentence each;
- decisions: what was decided or confirmed during the call;
- action_items: what participants must do, with the owner ("all teams", a person's name) and the
  due date when one was given (otherwise an empty string);
- key_moments: up to {MAX_KEY_MOMENTS} moments worth rewatching, with the time they start (H:MM:SS
  or MM:SS, as in the transcript) and a short topic.
Use only what was said. Never include phone numbers or links.
"""


class ActionItem(BaseModel):
    action: str
    owner: str
    due: str


class KeyMoment(BaseModel):
    time: str
    topic: str


class SessionRecap(BaseModel):
    summary: list[str]
    decisions: list[str]
    action_items: list[ActionItem]
    key_moments: list[KeyMoment]


def transcript_text(recording: Recording, segments) -> str:
    return "\n".join(f"[{format_offset(s.sent_at - recording.recorded_at)}] {s.author}: {s.text}" for s in segments)


def _words(text: str) -> set[str]:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z]+|\d+", ascii_text))


STOPWORDS = {"the", "and", "of", "a", "de", "la", "le", "du", "et", "session", "call", "recording"}
MONTHS = {"sep": 9, "sept": 9, "september": 9, "septembre": 9, "oct": 10, "october": 10, "octobre": 10}


def match_recordings(text: str, recordings: list[Recording]) -> list[Recording]:
    """Recordings the message refers to (all tied for best): by distinctive title words, or by date.

    A word found in every title ("Wadhwani", "Ignite") cannot tell sessions apart; any other can
    ("coaching", "welcome", or the "1" of "Module 1").
    """
    words = _words(text)
    titles = {r.id: _words(r.title) - STOPWORDS for r in recordings}
    generic = set.intersection(*titles.values()) if len(titles) > 1 else set()
    scored = []
    for recording in recordings:
        score = len((titles[recording.id] - generic) & words)
        day = recording.recorded_at.astimezone(timezone.utc)
        if str(day.day) in words and any(MONTHS.get(w) == day.month for w in words):
            score += 2
        if score:
            scored.append((score, recording))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [recording for score, recording in scored if score == best]


# A question about what happened in a session ("what questions were asked during the MIT call?"),
# not about a session to come ("when is the next MIT session?").
ABOUT_WHAT_WAS_SAID = re.compile(
    r"\b(during|in the|at the|lors d[ue]s?|pendant|au cours d[ue]s?|dans (le|la|les))\b"
    r"|\b(said|asked|explained|discussed|mentioned|answered|dit|parl[ée]|pos[ée]e?s?|expliqu[ée]|mentionn[ée]|[ée]voqu[ée]|abord[ée]|répondu)\b",
    re.IGNORECASE,
)
MOMENTS_QUOTED = 3

SESSION_QUESTION_SYSTEM = f"""\
A member of a WhatsApp community asks about a recorded call. Answer from its transcript alone, as a
colleague who attended would: directly, in a few sentences or a short list, in {{language}}.
{PROGRAMMES}
- answered: false when the transcript does not answer the question; never guess.
- moments: the numbers [n] of the 1 to {MOMENTS_QUOTED} transcript lines that best support the answer.
- Speaker names come from the screen and may be wrong (the host's name is often given to whoever
  speaks): name a person only if they introduce themselves or are called by name.
"""


class SessionAnswer(BaseModel):
    answered: bool
    answer: str
    moments: list[int]


class Recaps:
    def __init__(self, store: Store, llm: LLM):
        self.store = store
        self.llm = llm

    async def answer(self, question: str, language: str) -> Reply | None:
        """A question about what was said in one recorded session, named by its title: answered from
        the whole transcript (a search finds passages, not "the questions asked in the meeting"), with
        the moments it comes from. None when the question is not one, or the call does not answer it."""
        if not (SESSION_WORD.search(question) and ABOUT_WHAT_WAS_SAID.search(question)):
            return None
        recordings = [r for r in await self.store.all_recordings() if r.method != "link"]
        matches = match_recordings(question, recordings) if recordings else []
        if len(matches) != 1:
            return None
        recording = matches[0]
        segments = await self.store.messages_of(recording.id)
        if not segments:
            return None
        lines = "\n".join(
            f"[{n}] {format_offset(s.sent_at - recording.recorded_at)} {s.author}: {s.text}" for n, s in enumerate(segments, 1)
        )
        prompt = f"Call: «{recording.title}», {recording.recorded_at:%d %B %Y}.\n\nTranscript:\n{lines}\n\nQuestion: {question}"
        system = SESSION_QUESTION_SYSTEM.format(language=LANGUAGES[language])
        try:
            found = await self.llm.generate(prompt, SessionAnswer, system=system, timeout=SESSION_QUESTION_TIMEOUT, temperature=0.2)
        except LLMUnavailable:
            return None
        if not found.answered or not found.answer.strip():
            return None
        moments = [segments[n - 1] for n in dict.fromkeys(found.moments) if 1 <= n <= len(segments)][:MOMENTS_QUOTED]
        # A YouTube link starts at each moment; any other link is the same for all: given once, last.
        per_moment = is_youtube(recording.source_url or "")
        quoted = [
            recording_quote(recording, s.sent_at - recording.recorded_at, s.author, s.text, link=per_moment or s is moments[-1])
            for s in moments
        ]
        return Reply(found.answer.strip() + "".join(f"\n\n{q}" for q in quoted))

    async def generate(self, recording: Recording, language: str = "en") -> dict:
        """Write and store the recap of a recording in one language."""
        segments = await self.store.messages_of(recording.id)
        prompt = (
            f"Call: «{recording.title}», {recording.recorded_at:%d %B %Y}.\n\nTranscript:\n"
            + transcript_text(recording, segments)
            + f"\n\nWrite every item in {LANGUAGES[language]}."
        )
        recap = await self.llm.generate(prompt, SessionRecap, system=SYSTEM, timeout=TIMEOUT_SECONDS, temperature=0.2)
        data = recap.model_dump()
        await self.store.save_recap(recording.id, language, data)
        return data

    async def reply(self, text: str, language: str) -> str | None:
        """The recap of the session a message refers to; the list of sessions when unclear;
        None when the message is about no recorded session (then it is a normal question)."""
        texts = TEXTS[language]
        recordings = await self.store.all_recordings()
        if not recordings:
            return None
        number = re.fullmatch(r"/recap\s+(\d+)", text.strip(), re.IGNORECASE)
        if number and 1 <= int(number.group(1)) <= len(recordings):
            matches = [recordings[int(number.group(1)) - 1]]
        else:
            matches = match_recordings(text, recordings)
        if len(matches) != 1:
            if not matches and not text.strip().lower().startswith("/recap"):
                return None
            listed = matches or recordings
            lines = [f"{recordings.index(r) + 1}. {r.title} ({r.recorded_at:%d %b %Y})" for r in listed]
            return texts["recap_choose"] + "\n" + "\n".join(lines)

        recording = matches[0]
        data = (recording.recap or {}).get(language)
        if data is None:
            try:
                data = await self.generate(recording, language)
            except LLMUnavailable:
                return texts["recap_unavailable"]
        return format_recap(recording, data, language)


def format_recap(recording: Recording, data: dict, language: str) -> str:
    texts = TEXTS[language]
    recap = SessionRecap.model_validate(data)
    header = f"🎥 {recording.title} · {recording.recorded_at.astimezone(timezone.utc):%d %b %Y}"
    if recording.duration_seconds:
        header += f" · {format_offset(timedelta(seconds=recording.duration_seconds))}"
    parts = [header]
    if recording.source_url and timestamped_link(recording.source_url, timedelta()):
        parts[0] += f"\n{recording.source_url}"
    if recap.summary:
        parts.append(texts["recap_summary"] + "\n" + "\n".join(f"• {p}" for p in recap.summary))
    if recap.decisions:
        parts.append(texts["catchup_decisions"] + "\n" + "\n".join(f"• {d}" for d in recap.decisions))
    if recap.action_items:
        items = [
            f"• {a.action}" + (f" — {a.owner}" if a.owner else "") + (f" · {a.due}" if a.due else "")
            for a in recap.action_items
        ]
        parts.append(texts["recap_actions"] + "\n" + "\n".join(items))
    moments = []
    for moment in recap.key_moments[:MAX_KEY_MOMENTS]:
        try:
            offset = parse_timestamp(moment.time)
        except ValueError:
            continue
        link = timestamped_link(recording.source_url, offset)
        moments.append(f"• {format_offset(offset)} {moment.topic}" + (f"\n  {link}" if link else ""))
    if moments:
        parts.append(texts["recap_moments"] + "\n" + "\n".join(moments))
    return "\n\n".join(parts)
