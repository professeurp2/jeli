"""Adding a recorded session (a call) to Jeli's knowledge, without a command line.

The team pastes a YouTube link on the dashboard, or an organiser shares the recording's link in a
group: Jeli transcribes it in the background (Gemini watches the video, window by window), stores
each moment so answers can quote it and link to that second of the video, and writes the session's
recap. Teams or Zoom recordings behind a sign-in cannot be fetched: post them on YouTube (unlisted is
fine) or share their transcript.
"""

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.answer.llm import LLM, LLMUnavailable
from app.answer.recaps import Recaps
from app.ingest.transcribe import Transcriber, format_offset, is_youtube
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


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60] or "session"


def shared_recording(message: IncomingMessage) -> str | None:
    """The YouTube link of a session's recording shared in a group, if the message is one."""
    if message.is_private:
        return None
    link = YOUTUBE_LINK.search(message.text)
    return link.group(0) if link and RECORDING_WORDS.search(message.text) else None


@dataclass
class SessionImport:
    url: str
    title: str
    recorded_at: datetime
    by: str
    state: str = "waiting"  # waiting, transcribing, learning, done, failed
    progress: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    task: asyncio.Task | None = None


class Sessions:
    def __init__(self, store: Store, transcription: LLM, recaps: Recaps | None, on_learned=None):
        self.store = store
        self.transcription = transcription
        self.recaps = recaps
        self.on_learned = on_learned  # e.g. index the new segments now
        self.imports: dict[str, SessionImport] = {}

    def start(self, url: str, title: str, recorded_at: datetime, by: str) -> SessionImport | None:
        """Transcribe a session in the background; None when it is already being added."""
        if not is_youtube(url):
            raise ValueError("only YouTube links can be transcribed from here")
        current = self.imports.get(url)
        if current and current.state in ("waiting", "transcribing", "learning"):
            return None
        job = SessionImport(url, " ".join(title.split())[:120] or "Recorded session", recorded_at, by)
        self.imports[url] = job
        job.task = asyncio.get_running_loop().create_task(self._run(job))
        return job

    def from_group(self, message: IncomingMessage) -> SessionImport | None:
        """A recording's link shared in a group: add that session, named after the message."""
        url = shared_recording(message)
        if not url or url in self.imports:
            return None
        title = " ".join(YOUTUBE_LINK.sub("", message.text).split())[:80] or f"Session shared by {message.author}"
        return self.start(url, title, message.sent_at, message.author)

    async def _run(self, job: SessionImport) -> None:
        recording_id = f"{RECORDING_PREFIX}{job.recorded_at:%Y-%m-%d}-{slugify(job.title)}"
        try:
            job.state = "transcribing"
            transcriber = Transcriber(self.transcription, progress=lambda text: setattr(job, "progress", text))
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
