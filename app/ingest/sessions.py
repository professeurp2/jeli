"""Adding a recorded session (a call) to Jeli's knowledge, on its own.

Organisers (the groups' admins, and the people the team lists) share each recording's link with a
description ("Here is the recording of today's Module 2 class"). When one of them posts a link, a
model reads the message and says whether it shares a session's recording, which session and which
day. A recording on YouTube, or on Google Drive shared with anyone with the link, is then
transcribed in the background (Gemini watches the video, window by window), each moment stored so
answers can quote it, and its recap written. A recording Jeli cannot watch (Teams, SharePoint, a
Drive file behind a sign-in) is still kept as a link, so Jeli can say where it is. The team can also
add a session from the dashboard.
"""

import asyncio
import logging
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

from pydantic import BaseModel

from app.answer.citations import is_ignored
from app.answer.llm import LLM, LLMUnavailable
from app.answer.recaps import Recaps
from app.ingest.transcribe import NotPublic, Transcriber, download_drive, drive_id, format_offset, is_youtube
from app.kb.indexer import RECORDING_PREFIX
from app.kb.store import Store
from app.models import IncomingMessage, Recording, StoredMessage

log = logging.getLogger(__name__)

YOUTUBE_LINK = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|live/)|youtu\.be/)[\w-]{6,}[^\s]*", re.IGNORECASE)
# A link shared as a session's recording, not any video.
RECORDING_WORDS = re.compile(
    r"\b(recording|recorded|replay|session|class|meeting|webinar|call|module|enregistrement|replay|séance|réunion|cours)\b",
    re.IGNORECASE,
)


LINK = re.compile(r"https?://\S+", re.IGNORECASE)

DETECT_SYSTEM = """\
You read one message posted in a WhatsApp community by one of its organisers. Say whether it shares
the recording of a session (a class, coaching, webinar, meeting, call) — not a promotional video, a
tutorial from elsewhere, or a link to join a live session. If it does: title, a short name for the
session as members would call it ("Wadhwani Ignite — Module 2 class"); session_day, the day the
session took place as YYYY-MM-DD, resolving "today" or "yesterday" from the message's date ("" if
unknown); url, the recording's link exactly as written.
"""


class RecordingShare(BaseModel):
    is_recording: bool
    title: str
    session_day: str
    url: str


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60] or "session"


DRIVE_LINK = re.compile(r"https?://(?:drive|docs)\.google\.com/\S+", re.IGNORECASE)


def watchable(url: str) -> bool:
    """A recording Jeli can watch itself: YouTube, or a Google Drive file."""
    return is_youtube(url) or drive_id(url) is not None


def shared_recording(message: IncomingMessage) -> str | None:
    """The YouTube or Drive link of a session's recording shared in a group, if the message is one."""
    if message.is_private:
        return None
    link = YOUTUBE_LINK.search(message.text) or DRIVE_LINK.search(message.text)
    if not link or not watchable(link.group(0)) or not RECORDING_WORDS.search(message.text):
        return None
    return link.group(0)


@dataclass
class SessionImport:
    url: str
    title: str
    recorded_at: datetime
    by: str
    state: str = "waiting"  # waiting, transcribing, learning, done, failed, link (kept, cannot be watched)
    progress: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    task: asyncio.Task | None = None


class Sessions:
    def __init__(self, store: Store, transcription: LLM, recaps: Recaps | None, on_learned=None, reader: LLM | None = None):
        self.store = store
        self.transcription = transcription
        self.reader = reader  # reads organisers' messages for shared recordings
        self.recaps = recaps
        self.on_learned = on_learned  # e.g. index the new segments now
        self.imports: dict[str, SessionImport] = {}

    def start(self, url: str, title: str, recorded_at: datetime, by: str) -> SessionImport | None:
        """Transcribe a session in the background; None when it is already being added."""
        if not watchable(url):
            raise ValueError("only YouTube and Google Drive links can be transcribed from here")
        current = self.imports.get(url)
        if current and current.state in ("waiting", "transcribing", "learning"):
            return None
        job = SessionImport(url, " ".join(title.split())[:120] or "Recorded session", recorded_at, by)
        self.imports[url] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    def from_group(self, message: IncomingMessage) -> SessionImport | None:
        """A recording's link shared in a group, recognised by its words: add that session."""
        url = shared_recording(message)
        if not url or url in self.imports:
            return None
        title = " ".join(DRIVE_LINK.sub("", YOUTUBE_LINK.sub("", message.text)).split())[:80] or f"Session shared by {message.author}"
        return self.start(url, title, message.sent_at, message.author)

    async def from_organiser(self, message: IncomingMessage) -> SessionImport | None:
        """An organiser posted a link: if the message shares a session's recording, add it — the
        video transcribed if Jeli can watch it, otherwise kept as a link."""
        if message.is_private or not LINK.search(message.text) or self.reader is None:
            return None
        prompt = f"Message posted on {message.sent_at:%A %d %B %Y} by {message.author}:\n{message.text[:3000]}"
        try:
            share = await self.reader.generate(prompt, RecordingShare, system=DETECT_SYSTEM, timeout=8, temperature=0, attempts=2)
        except LLMUnavailable:
            return self.from_group(message)  # the words alone, while the model is busy
        url = share.url.strip()
        if not share.is_recording or not url.startswith("http") or url in self.imports:
            return None
        try:
            day = date.fromisoformat(share.session_day.strip())
            recorded_at = datetime.combine(day, time(12, 0), timezone.utc)
        except ValueError:
            recorded_at = message.sent_at
        title = " ".join(share.title.split())[:120] or f"Session shared by {message.author}"
        if watchable(url):
            return self.start(url, title, recorded_at, message.author)
        return await self.keep_link(url, title, recorded_at, message.author)

    def from_history(self, messages: list[StoredMessage], organisers: set[str]) -> int:
        """Recordings the organisers shared in an imported chat history: their messages with a link
        are read like live ones, one after the other, in the background. Returns how many."""
        candidates = [
            m for m in messages if LINK.search(m.text) and organisers and is_ignored(m, organisers)
        ]
        if candidates and self.reader is not None:
            asyncio.get_running_loop().create_task(self._read_history(candidates))
        return len(candidates)

    async def _read_history(self, messages: list[StoredMessage]) -> None:
        known = {r.source_url for r in await self.store.all_recordings() if r.source_url}
        for stored in messages:
            if any(link.rstrip(".,)") in known for link in LINK.findall(stored.text)):
                continue
            message = IncomingMessage(
                "whatsapp", stored.chat_id, stored.id, stored.author, stored.text, stored.sent_at, False, False,
                author_id=stored.author_id,
            )
            try:
                await self.from_organiser(message)
            except ValueError:
                continue
            except Exception:
                log.exception("Could not check an imported message for a recording")

    async def keep_link(self, url: str, title: str, recorded_at: datetime, by: str) -> SessionImport:
        """A recording Jeli cannot watch: kept as a link, so Jeli can say where it is."""
        job = SessionImport(url, title, recorded_at, by, state="link", progress="Shared as a link Jeli cannot watch (sign-in needed)")
        job.finished_at = datetime.now(timezone.utc)
        self.imports[url] = job
        recording_id = f"{RECORDING_PREFIX}{recorded_at:%Y-%m-%d}-{slugify(title)}"
        existing = (await self.store.recordings([recording_id])).get(recording_id)
        if existing is None:
            await self.store.save_recording(
                Recording(id=recording_id, title=title, recorded_at=recorded_at, method="link", source_url=url)
            )
        return job

    def in_progress(self) -> list[SessionImport]:
        return [job for job in self.imports.values() if job.state in ("waiting", "transcribing", "learning")]

    async def _run(self, job: SessionImport) -> None:
        recording_id = f"{RECORDING_PREFIX}{job.recorded_at:%Y-%m-%d}-{slugify(job.title)}"
        try:
            job.state = "transcribing"
            report = lambda text: setattr(job, "progress", text)  # noqa: E731
            transcriber = Transcriber(self.transcription, progress=report)
            if drive_id(job.url):
                with tempfile.TemporaryDirectory(prefix="jeli-") as folder:
                    try:
                        path = await download_drive(job.url, Path(folder), progress=report)
                    except NotPublic:
                        await self.keep_link(job.url, job.title, job.recorded_at, job.by)
                        return
                    segments = await transcriber.transcribe(str(path))
            else:
                segments = await transcriber.transcribe(job.url)
            if not segments:
                raise ValueError("no speech found in this video")
            job.state, job.progress = "learning", f"{len(segments)} moments, {format_offset(segments[-1].offset)} long"
            recording = Recording(
                id=recording_id,
                title=job.title,
                recorded_at=job.recorded_at,
                method="gemini",
                source_url=job.url,
                duration_seconds=int(segments[-1].offset.total_seconds()),
            )
            await self.store.forget(recording_id)
            await self.store.save_recording(recording)
            await self.store.add_messages(
                [
                    StoredMessage(
                        id=f"{recording_id}:{index:05d}",
                        chat_id=recording_id,
                        source="recording",
                        author=segment.speaker,
                        sent_at=job.recorded_at + segment.offset,
                        text=segment.text,
                    )
                    for index, segment in enumerate(segments)
                ]
            )
            if self.on_learned:
                await self.on_learned()
            if self.recaps:
                try:
                    await self.recaps.generate(recording, "en")
                except LLMUnavailable:
                    log.warning("Recap of %s not written yet: no model available", job.title)
            job.state = "done"
            job.progress = f"{len(segments)} moments, {format_offset(segments[-1].offset)} long — members can ask about it"
            await self.store.add_audit(job.by.lower(), f"Added the session “{job.title}”")
        except asyncio.CancelledError:
            job.state, job.progress = "failed", "Stopped"
            raise
        except Exception as error:
            log.exception("Adding the session %s failed", job.title)
            job.state = "failed"
            job.progress = str(error) if isinstance(error, ValueError) else "Something went wrong; try again later"
        finally:
            job.finished_at = datetime.now(timezone.utc)
