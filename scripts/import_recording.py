"""Import a call recording into Jeli's knowledge base (R2).

    python -m scripts.import_recording https://youtu.be/6q4uPBO_sDc --title "Module 1 class session" --date 2026-09-15
    python -m scripts.import_recording data/recordings/session.mp4 --title "…" --date 2026-09-16
    python -m scripts.import_recording data/recordings/teams-transcript.vtt --title "…" --date 2026-09-16

Sources: a YouTube link (Gemini watches it directly), an audio or video file (uploaded to Gemini),
or a transcript exported from Teams or Zoom (.vtt, .srt: parsed locally, no quota used).
The transcript is cached in data/transcripts/, so importing again never transcribes twice.
Importing a recording again replaces its previous version.
"""

import argparse
import json
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.answer.llm import LLM, LLMUnavailable
from app.answer.recaps import Recaps
from app.config import get_settings
from app.ingest.transcribe import Progress, Segment, Transcriber, format_offset, is_youtube, parse_subtitles
from app.kb.embeddings import Embedder
from app.kb.indexer import RECORDING_PREFIX, index_pending
from app.kb.store import Store
from app.models import Recording, StoredMessage
from scripts.common import require, run

CACHE = Path("data/transcripts")
SUBTITLES = {".vtt", ".srt"}


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:60]


def load_cache(path: Path) -> Progress | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text("utf-8"))
    segments = [Segment(timedelta(seconds=s["offset"]), s["speaker"], s["text"]) for s in data["segments"]]
    return Progress(segments, data["next_window"], data["silent_windows"], data["complete"])


def save_cache(path: Path, state: Progress) -> None:
    """Written after every window: an interrupted transcription resumes where it stopped."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "complete": state.complete,
        "next_window": state.next_window,
        "silent_windows": state.silent_windows,
        "segments": [{"offset": s.offset.total_seconds(), "speaker": s.speaker, "text": s.text} for s in state.segments],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")


async def get_segments(source: str, cache: Path, retranscribe: bool) -> tuple[list[Segment], str]:
    if Path(source).suffix.lower() in SUBTITLES:
        return parse_subtitles(Path(source).read_text("utf-8-sig")), "subtitles"
    state = None if retranscribe else load_cache(cache)
    if state and state.complete:
        print(f"Using the cached transcript {cache}")
        return sorted(state.segments, key=lambda s: s.offset), "gemini"
    if state:
        print(f"Resuming the transcription at window {state.next_window + 1} ({len(state.segments)} segments saved)")
    settings = get_settings()
    key = require(settings.gemini_api_key, "GEMINI_API_KEY")
    transcriber = Transcriber(
        LLM(key, settings.transcription_model_list), progress=print, checkpoint=lambda s: save_cache(cache, s)
    )
    return await transcriber.transcribe(source, resume=state), "gemini"


async def main(args: argparse.Namespace) -> None:
    recorded_at = datetime.combine(args.date, args.time, tzinfo=ZoneInfo(args.timezone))
    recording_id = f"{RECORDING_PREFIX}{args.date:%Y-%m-%d}-{slugify(args.title)}"
    if not is_youtube(args.source) and not Path(args.source).exists():
        raise SystemExit(f"Not found: {args.source}")

    segments, method = await get_segments(args.source, CACHE / f"{recording_id.removeprefix(RECORDING_PREFIX)}.json", args.retranscribe)
    if not segments:
        raise SystemExit("No speech found in this recording")
    speakers = sorted({s.speaker for s in segments})
    print(f"{len(segments)} segments, {format_offset(segments[-1].offset)} long, {len(speakers)} speakers: {', '.join(speakers[:8])}")

    if args.dry_run:
        for segment in segments[:5]:
            print(f"  [{format_offset(segment.offset)}] {segment.speaker}: {segment.text[:120]}")
        print("Dry run: nothing stored.")
        return

    recording = Recording(
        id=recording_id,
        title=args.title,
        recorded_at=recorded_at,
        method=method,
        source_url=args.source if is_youtube(args.source) else Path(args.source).name,
        duration_seconds=int(segments[-1].offset.total_seconds()),
    )
    messages = [
        StoredMessage(
            id=f"{recording_id}:{index:05d}",
            chat_id=recording_id,
            source="recording",
            author=segment.speaker,
            sent_at=recorded_at + segment.offset,
            text=segment.text,
        )
        for index, segment in enumerate(segments)
    ]

    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    embedder = Embedder(require(settings.gemini_api_key, "GEMINI_API_KEY"))
    await store.open()
    try:
        replaced, _ = await store.forget(recording_id)
        await store.save_recording(recording)
        await store.add_messages(messages)
        created = await index_pending(store, embedder)
        print(f"Stored {len(messages)} segments as {recording_id}{f' (replacing {replaced})' if replaced else ''}; {created} chunks indexed")
        if not args.no_recap:
            recaps = Recaps(store, LLM(settings.gemini_api_key, settings.answer_models))
            try:
                await recaps.generate(recording, "en")
                print("Recap written (ask Jeli: /recap)")
            except LLMUnavailable:
                print("Recap not written (no model available): run python -m scripts.recap_recording later")
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="YouTube link, audio/video file, or .vtt/.srt transcript")
    parser.add_argument("--title", required=True, help="e.g. 'Wadhwani Ignite — Module 1 class session'")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="day of the session, YYYY-MM-DD")
    parser.add_argument("--time", type=time.fromisoformat, default=time(12, 0), help="start time, HH:MM (default 12:00)")
    parser.add_argument("--timezone", default=get_settings().export_timezone)
    parser.add_argument("--retranscribe", action="store_true", help="ignore the cached transcript")
    parser.add_argument("--dry-run", action="store_true", help="transcribe (or read the cache) but store nothing")
    parser.add_argument("--no-recap", action="store_true", help="don't write the session recap")
    run(main(parser.parse_args()))
