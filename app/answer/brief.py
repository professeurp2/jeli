"""The community brief: Jeli's general knowledge of the group, always at hand.

Measured (20–21 Sep): "what programme?", "who is Diane?", "is that daily summary yours?" and
"what is Wadhwani?" went to a vector search and often came back "I don't know", while the answer
sat in the programme documents, the organisers' announcements and the session recaps. Every few
hours a model rewrites, from those, a short brief (programmes, organisers, key dates and rules,
sessions held, other bots in test); it is stored and given to every prompt as background. It is
background, not a source: answers still quote the messages.
"""

import logging
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.answer.citations import display_author, is_ignored, short_day
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import PROGRAMMES
from app.kb.store import Store

log = logging.getLogger(__name__)

KEY = "community"
MAX_WORDS = 380
ORGANISER_DAYS = 30
MAX_ORGANISER_MESSAGES = 120
MAX_MESSAGE_CHARS = 400
MAX_DOCUMENT_CHARS = 2500
MAX_DOCUMENTS = 8
TIMEOUT_SECONDS = 60

SYSTEM = f"""\
You write the background brief that Jeli, the assistant of a WhatsApp community (the UniPods METI AI
Innovation Programme, cohort 1), keeps in mind when it talks with members.
{PROGRAMMES}

From the material below (programme documents, organisers' announcements, session recaps), write a
brief of at most {MAX_WORDS} words, in English, as plain facts a colleague would know by heart:
- the programmes and what each one is (who runs it, its purpose, how it works), and how they relate;
- the organisers and their roles (first names only, never phone numbers);
- the key rules and dates that still matter (deadlines, team rules, how judging works), each with
  its date and who announced it;
- the sessions already held (title, day) and the sessions announced;
- anything members keep asking about (test slots, bots being tested in the group, tools to use).
Use only what the material says; never invent; prefer the most recent announcement when two
disagree and say what changed. Short lines, one fact per line, no headings.
"""


class CommunityBrief(BaseModel):
    brief: str


class Brief:
    def __init__(self, store: Store, llm: LLM | None):
        self.store = store
        self.llm = llm
        self.text = ""
        self.updated_at: datetime | None = None
        self.organisers: set[str] = set()  # set from the settings
        self.other_bots: list[str] = []  # ignored authors, named so that Jeli can say they are not it

    async def load(self) -> str:
        if hasattr(self.store, "load_brief"):
            saved = await self.store.load_brief(KEY)
            if saved:
                self.text, self.updated_at = saved["text"], saved["updated_at"]
        return self.text

    async def material(self) -> str:
        now = datetime.now(timezone.utc)
        parts = []
        documents = await self.store.list_documents()
        for document in documents[:MAX_DOCUMENTS]:
            pages = await self.store.messages_of(document.id)
            text = " ".join(" ".join(p.text.split()) for p in pages)[:MAX_DOCUMENT_CHARS]
            parts.append(f"Document «{document.title}» (shared by {display_author(document.shared_by)}, {short_day(document.shared_at)}):\n{text}")
        recordings = await self.store.all_recordings()
        for recording in recordings:
            recap = (recording.recap or {}).get("en") or {}
            summary = "; ".join(recap.get("summary") or [])
            decisions = "; ".join(recap.get("decisions") or [])
            line = f"Session «{recording.title}» held on {short_day(recording.recorded_at)}"
            if recording.method == "link":
                line += " (recording shared as a link only)"
            if summary:
                line += f". Summary: {summary}"
            if decisions:
                line += f". Decisions: {decisions}"
            parts.append(line)
        messages = await self.store.messages_since(now - timedelta(days=ORGANISER_DAYS))
        announcements = [m for m in messages if self.organisers and is_ignored(m, self.organisers)]
        for m in announcements[-MAX_ORGANISER_MESSAGES:]:
            parts.append(f"[{short_day(m.sent_at)}] {display_author(m.author)} (organiser): {' '.join(m.text.split())[:MAX_MESSAGE_CHARS]}")
        if self.other_bots:
            parts.append("Other bots being tested in the groups (not Jeli): " + ", ".join(display_author(b) for b in self.other_bots))
        return "\n\n".join(parts)

    async def refresh(self) -> str:
        """Rewrite and store the brief. Returns what happened, in words for the team."""
        if self.llm is None:
            return "no model available"
        material = await self.material()
        if not material.strip():
            return "nothing to write the brief from yet"
        prompt = f"Today is {datetime.now(timezone.utc):%A %d %B %Y}.\n\nMaterial:\n{material}\n\nWrite the brief."
        try:
            result = await self.llm.generate(prompt, CommunityBrief, system=SYSTEM, timeout=TIMEOUT_SECONDS, temperature=0.1)
        except LLMUnavailable:
            return "no model available; kept the previous brief"
        text = result.brief.strip()
        if not text:
            return "the model wrote nothing; kept the previous brief"
        words = text.split()
        if len(words) > MAX_WORDS + 60:
            text = " ".join(words[: MAX_WORDS + 60])
        self.text, self.updated_at = text, datetime.now(timezone.utc)
        if hasattr(self.store, "save_brief"):
            await self.store.save_brief(KEY, text)
        log.info("Community brief rewritten: %d words", len(words))
        return f"brief rewritten ({len(words)} words)"
