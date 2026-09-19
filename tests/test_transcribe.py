import asyncio
from datetime import timedelta

from google.genai import errors

from app.answer.llm import LLMUnavailable
from app.ingest.transcribe import (
    Progress,
    Segment,
    Transcriber,
    TranscriptSegment,
    TranscriptWindow,
    format_offset,
    is_youtube,
    parse_subtitles,
    parse_timestamp,
)

TEAMS_VTT = """\
WEBVTT

0f1c2d3e-1
00:00:03.520 --> 00:00:05.100
<v Charles Botom>Hi everyone,</v>

0f1c2d3e-2
00:00:05.100 --> 00:00:07.840
<v Charles Botom>can you hear me?</v>

0f1c2d3e-3
00:00:08.000 --> 00:00:10.000
<v Awa Traoré>Yes, loud and clear.</v>
"""

SRT = """\
1
00:01:02,500 --> 00:01:04,000
Awa: Welcome to Module 1.

2
00:01:05,000 --> 00:01:07,000
Let's start.
"""


def test_timestamps():
    assert parse_timestamp("12:34") == timedelta(minutes=12, seconds=34)
    assert parse_timestamp("1:02:03") == timedelta(hours=1, minutes=2, seconds=3)
    assert parse_timestamp("00:00:03,520") == timedelta(seconds=3.52)
    assert format_offset(timedelta(minutes=12, seconds=34)) == "12:34"
    assert format_offset(timedelta(hours=1, seconds=5)) == "1:00:05"


def test_teams_transcript_merges_consecutive_cues_of_a_speaker():
    assert parse_subtitles(TEAMS_VTT) == [
        Segment(timedelta(seconds=3.52), "Charles Botom", "Hi everyone, can you hear me?"),
        Segment(timedelta(seconds=8), "Awa Traoré", "Yes, loud and clear."),
    ]


def test_srt_transcript_with_speaker_prefixes():
    segments = parse_subtitles(SRT)
    assert segments[0] == Segment(timedelta(minutes=1, seconds=2.5), "Awa", "Welcome to Module 1.")
    assert segments[1].speaker == "Speaker"


def test_youtube_urls():
    assert is_youtube("https://youtu.be/6q4uPBO_sDc")
    assert is_youtube("https://www.youtube.com/watch?v=6q4uPBO_sDc")
    assert not is_youtube("https://drive.google.com/file/d/xyz/view")
    assert not is_youtube("data/recordings/module1.mp4")


class FakeLLM:
    """One reply per 15-minute window (or an exception to raise); 400 when asked past the end."""

    def __init__(self, windows):
        self.windows = list(windows)
        self.prompts = []

    async def generate(self, contents, schema, **kwargs):
        media, prompt = contents
        self.prompts.append((media, prompt))
        if not self.windows:
            raise errors.ClientError(400, {"error": {"message": "offset beyond video duration"}})
        outcome = self.windows.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def window(*items):
    return TranscriptWindow(segments=[TranscriptSegment(start=s, speaker=p, text=t) for s, p, t in items])


async def no_budget(tokens):
    pass


async def no_wait(seconds):
    pass


def transcribe(llm, source="https://youtu.be/6q4uPBO_sDc", checkpoints=None, resume=None):
    transcriber = Transcriber(
        llm, progress=lambda message: None, sleep=no_wait,
        checkpoint=lambda state: checkpoints.append((state.next_window, len(state.segments), state.complete))
        if checkpoints is not None else None,
    )
    transcriber.budget.spend = no_budget
    return asyncio.run(transcriber.transcribe(source, resume=resume))


def test_youtube_video_is_transcribed_window_by_window_until_it_ends():
    llm = FakeLLM([
        window(("0:00:07", "Charles Botom", "Hi, can you hear me?"), ("0:14:50", "Speaker 1", "Yes.")),
        window(("0:15:10", "Charles Botom", "Let's look at Module 1.")),
    ])
    segments = transcribe(llm)
    assert [(format_offset(s.offset), s.speaker) for s in segments] == [
        ("00:07", "Charles Botom"), ("14:50", "Speaker 1"), ("15:10", "Charles Botom"),
    ]
    first_media, _ = llm.prompts[0]
    second_media, second_prompt = llm.prompts[1]
    # Only the window is sent, at a low frame rate.
    assert (first_media.video_metadata.start_offset, first_media.video_metadata.end_offset) == ("0s", "900s")
    assert second_media.video_metadata.start_offset == "900s"
    assert first_media.video_metadata.fps == 0.1
    # Names found so far are passed on, to stay consistent.
    assert "Charles Botom, Speaker 1" in second_prompt and "between 15:00 and 30:00" in second_prompt


def test_offsets_counted_from_the_window_are_shifted():
    llm = FakeLLM([window(("0:00:05", "A", "one")), window(("0:00:05", "A", "two"))])
    segments = transcribe(llm)
    assert [s.offset for s in segments] == [timedelta(seconds=5), timedelta(minutes=15, seconds=5)]


def test_progress_is_saved_after_every_window():
    checkpoints = []
    transcribe(FakeLLM([window(("0:01:00", "A", "one")), window(("0:16:00", "A", "two"))]), checkpoints=checkpoints)
    assert checkpoints == [(1, 1, False), (2, 2, False), (2, 2, True)]


def test_an_interrupted_transcription_resumes_where_it_stopped():
    saved = Progress(segments=[Segment(timedelta(minutes=1), "A", "one")], next_window=1)
    llm = FakeLLM([window(("0:16:00", "A", "two"))])
    segments = transcribe(llm, resume=saved)
    assert [s.text for s in segments] == ["one", "two"]
    first_media, _ = llm.prompts[0]
    assert first_media.video_metadata.start_offset == "900s"


def test_transient_failures_are_retried():
    llm = FakeLLM([LLMUnavailable(), window(("0:01:00", "A", "one"))])
    assert len(transcribe(llm)) == 1


def test_a_window_that_keeps_failing_after_the_first_is_taken_as_the_end():
    llm = FakeLLM([window(("0:01:00", "A", "one")), LLMUnavailable(), LLMUnavailable(), LLMUnavailable()])
    assert len(transcribe(llm)) == 1


def test_two_silent_windows_end_the_recording():
    llm = FakeLLM([window(("0:01:00", "A", "hello")), window(), window(), window(("0:50:00", "A", "never asked"))])
    assert len(transcribe(llm)) == 1
    assert len(llm.prompts) == 3
