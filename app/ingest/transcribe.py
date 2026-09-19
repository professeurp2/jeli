"""Transcripts of call recordings (R2): YouTube videos, audio/video files, or existing subtitles.

Gemini listens to a recording in 15-minute windows. For videos only the window is processed
(measured: ~1,900 tokens per minute at low resolution and 0.1 frame per second, which is still
enough to read speaker names shown on screen by Teams). Speaker names found in a window are passed
on to the next one so they stay consistent. Transcripts exported from Teams or Zoom (.vtt, .srt)
are parsed directly: more accurate, and free.
"""

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from google.genai import errors, types
from pydantic import BaseModel

from app.answer.llm import LLM
from app.kb.embeddings import TokenBudget

log = logging.getLogger(__name__)

WINDOW = timedelta(minutes=15)
MAX_WINDOWS = 24  # 6 hours
WINDOW_TIMEOUT_SECONDS = 300
FRAMES_PER_SECOND = 0.1
TOKENS_PER_MINUTE_OF_MEDIA = 2_000
TOKENS_PER_MINUTE_BUDGET = 200_000
YOUTUBE = re.compile(r"^https?://(www\.|m\.)?(youtube\.com/(watch\?v=|live/|shorts/)|youtu\.be/)[\w-]{6,}")

PROMPT = """\
Transcribe the speech in this recording between {start} and {end}.
- Keep the original language (English or French), word for word, without filler words (uh, um).
- One segment per speaker turn; split long turns into segments of about 30 seconds.
- "start" is when the segment begins, as H:MM:SS counted from the beginning of the whole recording.
- Speakers: use the person's name when it is shown on screen or said, otherwise "Speaker 1", "Speaker 2", etc.
{known_speakers}- If nobody speaks between {start} and {end}, or the recording ends before {start}, return no segments.
"""


class TranscriptSegment(BaseModel):
    start: str
    speaker: str
    text: str


class TranscriptWindow(BaseModel):
    segments: list[TranscriptSegment]


@dataclass(frozen=True)
class Segment:
    offset: timedelta  # from the beginning of the recording
    speaker: str
    text: str


def parse_timestamp(value: str) -> timedelta:
    """'12:34', '1:02:03', '00:12:34.560' or '00:12:34,560' -> timedelta."""
    parts = [float(p) for p in re.split(r"[:]", value.strip().replace(",", "."))]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + part
    return timedelta(seconds=seconds)


def format_offset(offset: timedelta) -> str:
    total = int(offset.total_seconds())
    hours, rest = divmod(total, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"


def is_youtube(source: str) -> bool:
    return bool(YOUTUBE.match(source))


def _window_segments(window: TranscriptWindow, start: timedelta) -> list[Segment]:
    segments = []
    for item in window.segments:
        text = " ".join(item.text.split())
        if not text:
            continue
        try:
            offset = parse_timestamp(item.start)
        except ValueError:
            offset = start
        segments.append(Segment(offset=offset, speaker=item.speaker.strip() or "Speaker", text=text))
    # Some answers count from the start of the window rather than of the recording: shift them.
    if start and segments and max(s.offset for s in segments) < start - timedelta(seconds=30):
        segments = [Segment(s.offset + start, s.speaker, s.text) for s in segments]
    return segments


class Transcriber:
    def __init__(self, llm: LLM, budget: TokenBudget | None = None, progress: Callable[[str], None] = log.info):
        self.llm = llm
        self.budget = budget or TokenBudget(TOKENS_PER_MINUTE_BUDGET)
        self.progress = progress

    async def transcribe(self, source: str) -> list[Segment]:
        """A YouTube URL, or the path of an audio or video file."""
        if is_youtube(source):
            return await self._transcribe_media(source, "video/*", clip=True)
        uploaded = await self._upload(Path(source))
        return await self._transcribe_media(uploaded.uri, uploaded.mime_type, clip=uploaded.mime_type.startswith("video/"))

    async def _upload(self, path: Path) -> types.File:
        self.progress(f"Uploading {path.name} to Gemini…")
        uploaded = await self.llm.client.aio.files.upload(file=path)
        while uploaded.state == types.FileState.PROCESSING:
            await asyncio.sleep(5)
            uploaded = await self.llm.client.aio.files.get(name=uploaded.name)
        if uploaded.state != types.FileState.ACTIVE:
            raise RuntimeError(f"Gemini could not process {path.name}: {uploaded.state}")
        return uploaded

    async def _transcribe_media(self, uri: str, mime_type: str, clip: bool) -> list[Segment]:
        segments: list[Segment] = []
        speakers: list[str] = []
        silent_windows = 0
        for index in range(MAX_WINDOWS):
            start, end = WINDOW * index, WINDOW * (index + 1)
            # For videos, only this window is processed (and counted against the quota).
            window_metadata = types.VideoMetadata(
                start_offset=f"{int(start.total_seconds())}s",
                end_offset=f"{int(end.total_seconds())}s",
                fps=FRAMES_PER_SECOND,
            )
            media = types.Part(
                file_data=types.FileData(file_uri=uri, mime_type=mime_type),
                video_metadata=window_metadata if clip else None,
            )
            known = f"- Speakers named so far: {', '.join(speakers)}. Keep exactly these names.\n" if speakers else ""
            prompt = PROMPT.format(start=format_offset(start), end=format_offset(end), known_speakers=known)
            await self.budget.spend(int(WINDOW.total_seconds() / 60 * TOKENS_PER_MINUTE_OF_MEDIA))
            try:
                window = await self.llm.generate(
                    [media, prompt],
                    TranscriptWindow,
                    timeout=WINDOW_TIMEOUT_SECONDS,
                    temperature=0,
                    media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                )
            except errors.ClientError as error:
                if index and error.code == 400:
                    break  # asked past the end of the video
                raise
            found = _window_segments(window, start)
            self.progress(f"{format_offset(start)}–{format_offset(end)}: {len(found)} segments")
            if not found:
                silent_windows += 1
                if silent_windows == 2:
                    break  # the recording is over
                continue
            silent_windows = 0
            segments.extend(found)
            speakers.extend(s.speaker for s in found if s.speaker not in speakers)
        return sorted(segments, key=lambda s: s.offset)


VTT_VOICE = re.compile(r"^<v\s+([^>]+)>(.*?)(?:</v>)?$", re.DOTALL)
NAME_PREFIX = re.compile(r"^([^:]{1,60}):\s+(.+)$", re.DOTALL)


def parse_subtitles(text: str) -> list[Segment]:
    """Segments from a WebVTT (Teams, Zoom, YouTube) or SRT transcript.

    Teams cut speech into many short cues: consecutive cues of one speaker are merged.
    """
    segments: list[Segment] = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line.strip() for line in block.strip().splitlines()]
        timing = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing is None:
            continue
        offset = parse_timestamp(lines[timing].split("-->")[0].strip().split(" ")[0])
        body = " ".join(lines[timing + 1 :]).strip()
        if not body:
            continue
        speaker = "Speaker"
        voice = VTT_VOICE.match(body)
        named = NAME_PREFIX.match(body)
        if voice:
            speaker, body = voice.group(1).strip(), voice.group(2)
        elif named:
            speaker, body = named.group(1).strip(), named.group(2)
        body = re.sub(r"<[^>]+>", "", body).strip()
        if not body:
            continue
        previous = segments[-1] if segments else None
        if previous and previous.speaker == speaker and len(previous.text) + len(body) < 600:
            segments[-1] = Segment(previous.offset, speaker, f"{previous.text} {body}")
        else:
            segments.append(Segment(offset, speaker, body))
    return segments
