"""What Jeli says when the groups hold no answer: never a bare "I don't know".

Jeli knows its own state — the sessions it has transcribed, is transcribing, or only has a link to;
the sessions and deadlines coming up; the documents it keeps; the day its memory of the groups stops
at. When a question finds nothing, it says so plainly and adds what is useful from that state: the
recording of that session is being transcribed (ready in a few minutes), the session is scheduled for
Tuesday, the recording is a Teams link it cannot watch (here it is), its memory of the groups stops
on Friday. It never invents anything about the programme: only its state is used.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from app.answer.citations import display_author, short_day
from app.config import get_settings
from app.answer.intents import SESSION_WORD
from app.answer.language import TEXTS
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import PERSONA, background
from app.answer.prompts import LANGUAGES
from app.answer.recaps import match_recordings
from app.kb.store import LIVE_WINDOW, Store
from app.models import Recording

log = logging.getLogger(__name__)

WINDOW_DONE = re.compile(r"–(\d+(?::\d+){1,2}):")

SYSTEM = PERSONA + """
Your task now: a member asked something you found nothing about in the groups' messages, the
recorded sessions or the shared documents. Write your reply, in one to three short sentences —
never a bare "I don't know". Say plainly that the groups haven't covered it (or not yet). Then add
only what is useful from your state below: a matching session being transcribed (ready soon),
scheduled (when), or shared as a link you cannot watch (give the link); the day your memory of the
groups stops at, only if your state says you are not connected to them; a relevant document or deadline. End
with one next step: ask the organisers, share the recording in the group, or try /search with a
keyword. For a general question unrelated to the community, say kindly that you only know what was
shared in it. If the background brief answers a general question about the community (what a
programme is, who runs it), answer from it in one sentence instead of saying you don't know.
Never invent anything about the programme, sessions, people or dates: use only what is given.
"""


class Explanation(BaseModel):
    reply: str


def progress_words(progress: str, language: str) -> str:
    """"0:00–30:00: 42 segments" → "the first 30 minutes are done"."""
    match = WINDOW_DONE.search(progress or "")
    if not match:
        return "it has just started" if language == "en" else "elle vient de commencer"
    parts = [int(p) for p in match.group(1).split(":")]
    minutes = parts[0] * 60 + parts[1] if len(parts) == 3 else parts[0]
    return f"the first {minutes} minutes are done" if language == "en" else f"les {minutes} premières minutes sont faites"


class Awareness:
    def __init__(self, store: Store, llm: LLM | None, sessions=None, chat_labels: dict[str, str] | None = None,
                 runtime=None):
        self.store = store
        self.llm = llm
        self.sessions = sessions
        self.chat_labels = chat_labels or {}
        self.runtime = runtime  # what the team has switched on; None: everything, as before
        self.brief = ""  # the community brief, background for the explanation

    async def explain(self, question: str, language: str, quotes: str = "", member: str = "") -> str:
        texts = TEXTS[language]
        # A session being transcribed that the question is about: say so, no model needed.
        jobs = self.sessions.in_progress() if self.sessions else []
        if jobs:
            pseudo = [Recording(id=job.url, title=job.title, recorded_at=job.recorded_at, method="gemini") for job in jobs]
            matched = match_recordings(question, pseudo)
            if not matched and len(jobs) == 1 and SESSION_WORD.search(question):
                matched = pseudo
            if matched:
                job = next(job for job in jobs if job.url == matched[0].id)
                return texts["session_in_progress"].format(
                    title=job.title, who=display_author(job.by), day=short_day(job.recorded_at),
                    progress=progress_words(job.progress, language),
                )
        if self.llm is None:
            return texts["dont_know"] + (f"\n\n{quotes}" if quotes else "")
        prompt = (
            background(self.brief, await self.state(), member)
            + "\n\n"
            + (f"The closest things the groups said (not an answer):\n{quotes}\n\n" if quotes else "")
            + f"Question (answer in {LANGUAGES.get(language, 'English')}):\n{question}"
        )
        try:
            explanation = await self.llm.generate(prompt, Explanation, system=SYSTEM, timeout=6, temperature=0.3, attempts=2)
        except LLMUnavailable:
            return texts["dont_know"] + (f"\n\n{quotes}" if quotes else "")
        reply = explanation.reply.strip() or texts["dont_know"]
        return reply + (f"\n\n{quotes}" if quotes else "")

    def _on(self, key: str) -> bool:
        """Whether the team has this switched on; True when there is no runtime to ask."""
        try:
            return bool(self.runtime[key])
        except Exception:
            return True

    async def state(self) -> str:
        now = datetime.now(timezone.utc)
        lines = [f"Today is {now:%A %d %B %Y}."]
        overview = await self.store.knowledge_overview()
        chats = overview["chats"]
        if chats:
            latest = max(c["last_message"] for c in chats)
            # In the groups when a group received live messages lately: a quiet night is not "offline".
            live = any(c["live"] and c["chat_id"].endswith("@g.us") and now - c["last_message"] < LIVE_WINDOW for c in chats)
            lines.append(
                f"Jeli's memory of the groups goes up to {latest:%A %d %B %Y, %H:%M} UTC; "
                + (
                    "it receives new messages live. A quiet period since then is only a quiet period: never say Jeli is "
                    "not in the group or that its memory stops."
                    if live
                    else "it is not connected to the groups yet, so it knows nothing said after that."
                )
            )
        domain = get_settings().railway_public_domain
        if domain and self._on("enabled.calls"):
            lines.append(
                f"Members can call you and talk out loud at https://{domain}/jeli/call — give that exact "
                "link when someone asks to speak to you or asks for the call link."
            )
        elif not self._on("enabled.calls"):
            # Switched off in the dashboard: Jeli must stop offering a line nobody can pick up.
            lines.append("You cannot take calls at the moment: there is no call link to give. Say so kindly.")
        recordings = await self.store.all_recordings()
        transcribed = [r for r in recordings if r.method != "link"]
        links = [r for r in recordings if r.method == "link"]
        if transcribed:
            lines.append("Sessions Jeli has transcribed and can quote: " + "; ".join(f"{r.title} ({short_day(r.recorded_at)})" for r in transcribed[-12:]))
        for job in self.sessions.in_progress() if self.sessions else []:
            lines.append(f"Being transcribed now: {job.title} ({short_day(job.recorded_at)}), shared by {display_author(job.by)} — {progress_words(job.progress, 'en')}.")
        for recording in links[-8:]:
            lines.append(f"Recording shared as a link Jeli cannot watch: {recording.title} ({short_day(recording.recorded_at)}) — {recording.source_url}")
        upcoming = await self.store.deadlines_between(now.date(), now.date() + timedelta(days=14))
        if upcoming:
            lines.append("Coming up: " + "; ".join(f"{d.what} ({short_day(datetime.combine(d.due_date, datetime.min.time(), timezone.utc))}{', ' + d.due_time if d.due_time else ''})" for d in upcoming[:12]))
        documents = await self.store.list_documents() if hasattr(self.store, "list_documents") else []
        if documents:
            lines.append("Documents Jeli keeps and can send: " + "; ".join(d.title for d in documents[:12]))
        if hasattr(self.store, "mentioned_documents"):
            from app.answer.documents import missing_documents

            missing = await missing_documents(self.store)
            if missing:
                lines.append(
                    "Documents shared in the groups whose file Jeli does not have (the chat history came without its files; "
                    "anyone can share them again): "
                    + "; ".join(f"{m['name']} (shared by {display_author(m['author'])}, {short_day(m['sent_at'])})" for m in missing[:10])
                )
        return "\n".join(lines)
