"""Voice notes, as WhatsApp members use them: Jeli listens to a question asked by voice, and answers
by voice — when it was asked by voice, or when the member asks for a voice reply.

Listening: the voice note goes to the answer model as audio, which writes down what was said.
Speaking: the answer is first said again in spoken language, with the mood of its content (joyful,
reassuring, calm). Then Gemini TTS says it, on every key in turn; Gemini's native-audio voice (the
Live API, outside the TTS models' 10-a-day quota) when no key can; edge-tts (free, no quota) only
when no Gemini voice answers. Each is told the same mood.
The audio bytes returned by speak() are self-describing: RIFF header = WAV (from Gemini PCM),
no RIFF header = MP3 (from edge-tts). WAHA has convert:True so it transcodes to OGG/Opus anyway.
When any step fails, the member gets the written answer instead: a voice reply is a courtesy.
"""

import asyncio
import hashlib
import io
import logging
import re
import time
import wave
from datetime import date, datetime, timedelta, timezone

import edge_tts
from google.genai import errors, types
from pydantic import BaseModel

from app.answer.language import detect_language
from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

# Gemini native audio (Live API) — the second voice. Measured on the key on 22 Sep 2026: first sound
# after 1.5 s, then streamed in real time (32 s of speech in 34 s); its quota is not the 10-a-day one
# of the TTS models below. Word for word on French texts, a question read and not answered — but a
# French text full of English names was once said in English: hence second, told the language.
LIVE_VOICE_MODELS = ["gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]
LIVE_MAX_CHARS = 900  # about a minute of speech; longer answers go to the faster TTS below
LIVE_FIRST_AUDIO_SECONDS = 8.0
CHARS_PER_SECOND = 15  # measured: 14–17 characters of French speech per second
LIVE_SYSTEM = """\
You are the voice of Jeli, a warm assistant of an African innovators' WhatsApp community, recording
a WhatsApp voice note. The text is in {language}: say it aloud in {language}, word for word, as a
native speaker would — never translate it, not even its English names or terms. Add nothing, drop
nothing, never answer it or comment on it, even when it asks a question. Sound like a real person
talking to a friend, not an announcer: relaxed, natural pauses, breathing, and the feeling of the
words. Say it {mood}.
"""
# The feeling of a voice note, given by the spoken rewrite: how each voice is asked to say it,
# and edge-tts's prosody (it cannot act, but a livelier or softer pace and pitch come through).
MOODS = {
    "joyful": ("with a big smile and real enthusiasm, like good news you are happy to share", "+6%", "+6Hz"),
    "reassuring": ("softly and kindly, reassuring, like helping a friend who is worried", "-6%", "-3Hz"),
    "calm": ("warmly and relaxed, like a friend explaining something simply", "+0%", "+0Hz"),
}
DEFAULT_MOOD = "calm"

# Gemini TTS — the first voice: expressive, reads the text as it is, faster than real time (34 s of
# speech made in 20 s, 22 Sep) — but 10 requests a day per project, hence every key in turn.
# Uses the same API key pool as the LLM (existing rotation in LLM._clients).
# Tried in order on every key (checked on the key on 21 Sep 2026: both exist; the free tier
# allows about 10 requests a day per key and model, hence the rotation and the edge-tts fallback).
# The voices tried before the backup voice (edge-tts), as the team chose on the dashboard: all the
# natural ones in turn, one of them, or none (the backup voice only: it spares the Gemini quota).
VOICE_ENGINES = {"auto": ("natural", "live"), "natural": ("natural",), "live": ("live",), "backup": ()}
GEMINI_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
GEMINI_TTS_MODEL = GEMINI_TTS_MODELS[0]
GEMINI_TTS_VOICES = {"fr": "Aoede", "en": "Aoede"}  # warm, expressive multilingual voice
GEMINI_TTS_TIMEOUT = 12.0  # per attempt, plus the time to say the text (a longer text takes longer)
GEMINI_TTS_SECONDS_PER_SPOKEN_SECOND = 0.8  # measured: 8.3 s of speech in 6.2 s, 34 s in 20 s
GEMINI_TTS_MAX_ATTEMPT_SECONDS = 60.0
# Measured 21 Sep: the 3.1 preview timed out on every key in turn (20 s each, before the fallback
# voice). Bounded: 30 s in all; two slow failures rest the model; a key over quota (429) rests alone.
GEMINI_TTS_TOTAL_SECONDS = 30.0
GEMINI_TTS_SLOW_FAILURES = 2
GEMINI_TTS_REST_SECONDS = 300
GEMINI_TTS_KEY_REST_SECONDS = 3600
# The free tier's daily quota, per project (key) and speech model (Google's 429 says "limit: 10"),
# renewed at midnight Pacific time. Jeli counts what it used, so the team sees what is left.
TTS_REQUESTS_PER_DAY = 10

# edge-tts — fallback when Gemini TTS quota is exhausted or unavailable.
# edge-tts (7.x) escapes what it is given and builds its own SSML: it must receive plain text,
# never SSML, or it reads the tags aloud ("Speak version 1.0 xmlns…").
# The "Multilingual" neural voices are the most natural ones edge-tts offers (checked 22 Sep
# with list_voices); Swahili and Amharic get their own voice instead of an English one.
EDGE_VOICES = {
    "fr": "fr-FR-VivienneMultilingualNeural",
    "en": "en-US-AvaMultilingualNeural",
    "sw": "sw-KE-ZuriNeural",
    "am": "am-ET-MekdesNeural",
}
AUDIO_MIMETYPE = "audio/mpeg"  # edge-tts output
AUDIO_BYTES_PER_SECOND = 16_000  # edge-tts MP3 at ~128 kbps (used for recording-delay timing)
SAMPLE_RATE = 24_000  # WAV helper: 16-bit mono PCM at 24 kHz (used by tests)
# edge-tts has no hard limit; cap at ~3 min of speech so voice notes stay listenable.
MAX_SPOKEN_CHARS = 3_000
MAX_VOICE_BYTES = 5 * 1024 * 1024  # a voice note of several minutes; longer is not a question

# "Réponds en vocal", "send me a voice note", "reply by audio"…
VOICE_REQUEST = re.compile(
    r"\b(?:en|par|avec un(?:e)?)\s+(?:vocal|audio|note vocale|message vocal)\b|\bvocalement\b|\bnote vocale\b|\bmessage vocal\b"
    r"|\bvoice\s+(?:note|message|reply|answer)\b|\b(?:in|by|with|as)\s+(?:voice|audio)\b|\baudio\s+(?:reply|answer|message)\b",
    re.IGNORECASE,
)
QUOTE_LINE = re.compile(r"^\s*>.*$", re.MULTILINE)
LINK = re.compile(r"https?://\S+")
MENTION = re.compile(r"(?<!\w)@\d{5,}\b")
# Technical identifiers that must never be read aloud: WhatsApp ids ("1203…@g.us") and phone numbers.
JID = re.compile(r"\b\d{5,}(?::\d+)?@[\w.]+")
PHONE = re.compile(r"\+\d[\d\s().-]{7,}\d")
EMOJI = re.compile("[\U0001f000-\U0001faff⌀-⏿☀-➿⬀-⯿️‍]")

LISTEN_SYSTEM = """\
Write down, word for word, what is said in this WhatsApp voice note, in the language it is spoken
(English, French or another). No comments, no timestamps; "" if nothing is said.
"""
_BULLET = re.compile(r"\n\s*[-•]\s*")
_NEWLINES = re.compile(r"\n+")
_PUNCT_ARTIFACTS = re.compile(r"[,;]\s*[.,;]|[.]\s*,|\s{2,}")


def for_speech(text: str) -> str:
    """Turn structured text (newlines, bullets) into natural spoken flow.
    Called on the output of spoken() before passing to the TTS engine."""
    text = _BULLET.sub(". ", text)          # "- item" → a pause, then the item
    text = _NEWLINES.sub(". ", text)        # paragraph/line breaks → pause
    text = _PUNCT_ARTIFACTS.sub(lambda m: m.group(0)[0], text)
    text = text.strip(" ,.")
    # Remove repeated 3+-word phrases (e.g. a date said four times in a catchup).
    seen: set[str] = set()
    words = text.split()
    out: list[str] = []
    i = 0
    while i < len(words):
        matched = False
        for n in (5, 4, 3):
            phrase = " ".join(words[i:i + n])
            if len(phrase) > 10 and phrase in seen:
                i += n
                matched = True
                break
        if not matched:
            if i + 3 <= len(words):
                seen.add(" ".join(words[i:i + 3]))
            out.append(words[i])
            i += 1
    return " ".join(out)
IMAGE_SYSTEM = """\
Describe what this image shows in 2-4 sentences. If it contains a table, list, chart, form or any
text, transcribe the key content faithfully. Be concise and factual — focus on information that
would help answer a question about the image.
"""


class Heard(BaseModel):
    text: str


class Script(BaseModel):
    text: str
    mood: str = DEFAULT_MOOD


LANG_LABELS = {
    "fr": "French", "en": "English", "sw": "Swahili",
    "rw": "Kinyarwanda", "ln": "Lingala", "wo": "Wolof", "am": "Amharic",
}
# A written answer read as it is sounds read, not spoken. Short answers are said again in spoken
# language, with the feeling of the content; long ones (digests, lists) are read as they are.
SPEAK_SYSTEM = """\
You are Jeli, the warm assistant griot of an African innovators' WhatsApp community. Rewrite the
written answer below as what you would say in a voice note to a friend.
- Entirely in the language you are given: say in it the parts written in another language (an
  English item in a French answer is said in French), keeping only proper names (people,
  programmes, platforms) as they are. Same facts. Keep every number, date and time exactly as
  written, digits as digits. Add nothing that is not in the answer.
- Speak, do not read: natural spoken rhythm, contractions, short sentences, no lists, no symbols,
  no emoji, no markdown, no links, no ids, no phone numbers.
- Summarise it as a person would tell it to a friend: a list becomes a few flowing sentences (the
  most pressing first); leave out what a listener does not need — who announced each item, where
  and when it was announced — but keep the day, date and time of each thing that is coming. Say
  days and months in full ("mardi 22 septembre", not "mar. 22 sept.").
- Let the feeling of the content come through: real enthusiasm for good news, a gentle reassuring
  tone for a problem or a "not found", calm warmth otherwise. A small human reaction at the start
  when it fits (a laugh, "ah,", "good news!"), never the same one twice in a row.
- No greeting and no goodbye unless the answer has one. No longer than the answer.
Return "text", and "mood": the feeling your voice should carry — "joyful" (good news, a success,
thanks, a welcome), "reassuring" (a problem, a delay, something missing or not found) or "calm"
(plain information).
"""
SPEAK_REWRITE_MAX_CHARS = 2000  # a digest or a list of deadlines is told, not read
REWRITE_ATTEMPTS = 2
_NUMBER = re.compile(r"\d+")


class Seen(BaseModel):
    description: str


VOICE_ASK = re.compile(
    r"[,;.!]?\s*(?:(?:please|stp|svp|merci de)\s+)?"
    r"(?:(?:réponds|répondez|répond|reply|answer|dis-le|say it|send it|envoie|send)(?:[- ]moi)?(?:\s+me)?\s+)?"
    r"(?:une?\s+|a\s+)?(?:" + VOICE_REQUEST.pattern + r")\s*(?:(?:please|stp|svp)\b)?[.!]?",
    re.IGNORECASE,
)


def asks_for_voice(text: str) -> bool:
    return bool(VOICE_REQUEST.search(text))


def without_voice_request(text: str) -> str:
    """The question, without "reply by voice": what Jeli has to answer."""
    return VOICE_ASK.sub("", text).strip(" ,;:") or text


def spoken(reply: str) -> str:
    """What a voice note says of a written reply: its words, without the quote blocks, links, mentions
    and WhatsApp formatting — those stay in the text sent with it."""
    text = MENTION.sub("", QUOTE_LINE.sub("", reply))
    text = PHONE.sub("", JID.sub("", LINK.sub("", text)))
    text = re.sub(r"[*_~`]", "", text)
    text = re.sub(r"\s*\(/\w+\)", "", text)  # "(/catchup)": a command to type, not to say
    text = re.sub(r"(?<!\S)/(\w+)", r"\1", text)
    text = EMOJI.sub("", text)
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip(" :").replace(" ,", ",")


def sources(reply: str) -> str:
    """The reply's quote blocks (its sources) and links: sent as text after the voice note."""
    kept = [line for line in reply.splitlines() if line.lstrip().startswith(">")]
    links = [link for link in LINK.findall(reply) if not any(link in line for line in kept)]
    return "\n".join(kept + links).strip()


def wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(rate)
        file.writeframes(pcm)
    return out.getvalue()


def _over_quota(error: Exception) -> bool:
    """A key whose quota is spent (HTTP 429, or the Live API closing with RESOURCE_EXHAUSTED)."""
    if isinstance(error, errors.APIError) and error.code == 429:
        return True
    text = str(error)
    return "RESOURCE_EXHAUSTED" in text or "quota" in text.lower()


def _pacific_offset(moment: datetime) -> timedelta:
    """Pacific time's offset from UTC at `moment`: -7 h from the second Sunday of March to the first
    Sunday of November (2 a.m. local), -8 h otherwise."""
    year = moment.year
    march = datetime(year, 3, 8, 10, tzinfo=timezone.utc)  # 2 a.m. PST
    start = march + timedelta(days=(6 - march.weekday()) % 7)
    november = datetime(year, 11, 1, 9, tzinfo=timezone.utc)  # 2 a.m. PDT
    end = november + timedelta(days=(6 - november.weekday()) % 7)
    return timedelta(hours=-7) if start <= moment < end else timedelta(hours=-8)


def quota_day(now: datetime | None = None) -> date:
    """The day Google's daily quotas count: the date in Pacific time."""
    now = now or datetime.now(timezone.utc)
    return (now + _pacific_offset(now)).date()


def quota_renewal(now: datetime | None = None) -> datetime:
    """When the daily quotas renew next (midnight Pacific time), in UTC."""
    now = now or datetime.now(timezone.utc)
    return datetime.combine(quota_day(now) + timedelta(days=1), datetime.min.time(), timezone.utc) - _pacific_offset(now)


def audio_mimetype(audio: bytes) -> str:
    """Detect the MIME type from the audio's magic bytes (no dependency on TTS engine used)."""
    return "audio/wav" if audio[:4] == b"RIFF" else AUDIO_MIMETYPE


def audio_seconds(audio: bytes) -> float:
    """Approximate duration: WAV 24 kHz 16-bit mono vs MP3 ~128 kbps."""
    if audio[:4] == b"RIFF":
        return len(audio) / (SAMPLE_RATE * 2)  # 2 bytes per sample, mono
    return len(audio) / AUDIO_BYTES_PER_SECOND


class Voice:
    def __init__(self, llm: LLM):
        self.llm = llm  # listens with the answer models; speaks with its client
        self.voice_name: str = "Aoede"  # overridden by Runtime (dashboard → apply.py)
        self._tts_resting: dict[str, float] = {}  # Gemini TTS model → monotonic time it is skipped until
        self._tts_key_resting: dict[tuple[int, str], float] = {}  # (key, model) over quota
        self._tts_cursor = 0  # next key to try, so the free quota is spread over the keys
        self._live_cursor = 0
        # Which voice speaks first (set from the dashboard): see VOICE_ENGINES.
        self.engine = "auto"
        # Today's use of the speech models' quota, per (key, model): kept in the database (store,
        # set at startup) so that a restart does not forget it.
        self.store = None
        self._quota_day: date | None = None
        self._used: dict[tuple[str, str], int] = {}
        self._exhausted: set[tuple[str, str]] = set()

    def _key_id(self, idx: int) -> str:
        """A key's fingerprint, never the key itself."""
        keys = getattr(self.llm, "_api_keys", None) or []
        return hashlib.sha256(keys[idx].encode()).hexdigest()[:12] if idx < len(keys) else f"key{idx}"

    async def _today(self) -> None:
        """Start the day's count (Google renews the quota at midnight Pacific time), from what the
        database kept of it."""
        day = quota_day()
        if day == self._quota_day:
            return
        self._quota_day, self._used, self._exhausted = day, {}, set()
        if self.store is not None and hasattr(self.store, "voice_quota"):
            try:
                for row in await self.store.voice_quota(day):
                    self._used[(row["key_id"], row["model"])] = row["used"]
                    if row["exhausted"]:
                        self._exhausted.add((row["key_id"], row["model"]))
            except Exception:
                log.exception("Could not read today's voice quota")

    async def _count(self, idx: int, model: str, exhausted: bool = False) -> None:
        """One voice note made on this key and model, or the key's quota found spent."""
        await self._today()
        pair = (self._key_id(idx), model)
        if exhausted:
            self._exhausted.add(pair)
        else:
            self._used[pair] = self._used.get(pair, 0) + 1
        if self.store is not None and hasattr(self.store, "count_voice"):
            try:
                await self.store.count_voice(self._quota_day, pair[0], model, exhausted=exhausted)
            except Exception:
                log.exception("Could not save the voice quota")

    async def quota(self) -> dict:
        """The natural voice notes left today, out of the day's total: every valid key gives
        TTS_REQUESTS_PER_DAY per speech model, so a key added (or refused) changes the total."""
        await self._today()
        clients = getattr(self.llm, "_clients", None) or []
        valid = self.llm.valid_keys() if hasattr(self.llm, "valid_keys") else range(len(clients))
        keys = [self._key_id(i) for i in valid]
        per_key = len(GEMINI_TTS_MODELS) * TTS_REQUESTS_PER_DAY
        used = sum(
            TTS_REQUESTS_PER_DAY if (key, model) in self._exhausted else min(self._used.get((key, model), 0), TTS_REQUESTS_PER_DAY)
            for key in keys
            for model in GEMINI_TTS_MODELS
        )
        total = len(keys) * per_key
        return {
            "total": total, "remaining": max(0, total - used), "used": used, "keys": len(keys), "per_key": per_key,
            "renews_at": quota_renewal(),
        }

    def quota_marker(self) -> str:
        """Changes when the count does: the dashboard refreshes then."""
        clients = getattr(self.llm, "_clients", None) or []
        return f"{self._quota_day}:{sum(self._used.values())}:{len(self._exhausted)}:{len(clients)}"

    async def listen(self, audio: bytes, mimetype: str) -> str | None:
        """What the member said. Returns "" when nothing was heard (silence/noise/oversized audio),
        None when the transcription model itself was unavailable (so the caller can notify the user)."""
        if not audio or len(audio) > MAX_VOICE_BYTES:
            return ""
        part = types.Part(inline_data=types.Blob(mime_type=mimetype.split(";")[0].strip() or "audio/ogg", data=audio))
        try:
            heard = await self.llm.generate([part, "Transcribe this voice note."], Heard, system=LISTEN_SYSTEM, timeout=20, temperature=0, attempts=2)
        except LLMUnavailable:
            log.warning("Could not listen to a voice note: no model available")
            return None
        return " ".join(heard.text.split())

    async def describe(self, image: bytes, mimetype: str) -> str:
        """What the image shows; "" when the model cannot process it."""
        if not image:
            return ""
        part = types.Part(inline_data=types.Blob(mime_type=mimetype.split(";")[0].strip() or "image/jpeg", data=image))
        try:
            seen = await self.llm.generate([part, "Describe this image."], Seen, system=IMAGE_SYSTEM, timeout=20, temperature=0, attempts=1)
        except LLMUnavailable:
            log.warning("Could not describe an image: no model available")
            return ""
        return seen.description.strip()

    async def _speak_live(self, text: str, mood: str = DEFAULT_MOOD, language: str = "en") -> bytes | None:
        """Gemini's native-audio voice (Live API) on every key in turn; WAV bytes, or None.
        A voice note whose length does not fit the text (the model answered it, or stopped) is
        not sent."""
        clients = getattr(self.llm, "_clients", None) or []
        if not clients or len(text) > LIVE_MAX_CHARS:
            return None
        voice_name = (self.voice_name or "Aoede").title()
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=LIVE_SYSTEM.format(
                mood=MOODS.get(mood, MOODS[DEFAULT_MOOD])[0], language=LANG_LABELS.get(language, "English")
            ),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name))
            ),
        )
        expected = len(text) / CHARS_PER_SECOND
        for model in LIVE_VOICE_MODELS:
            if self._tts_resting.get(model, 0.0) > time.monotonic():
                continue
            slow = 0
            for step in range(len(clients)):
                idx = (self._live_cursor + step) % len(clients)
                if self._tts_key_resting.get((idx, model), 0.0) > time.monotonic():
                    continue
                try:
                    pcm = await self._live_once(clients[idx], model, config, text, expected)
                except Exception as error:
                    if _over_quota(error):
                        self._tts_key_resting[(idx, model)] = time.monotonic() + GEMINI_TTS_KEY_REST_SECONDS
                        continue  # this key's quota is spent: the next key may have some left
                    log.warning("Gemini voice %s key %d failed: %s: %s", model, idx, type(error).__name__, error)
                    slow += 1
                    if slow >= GEMINI_TTS_SLOW_FAILURES:
                        break
                    continue
                seconds = len(pcm) / (SAMPLE_RATE * 2)
                if 0.45 * expected - 1 <= seconds <= 2.2 * expected + 3:
                    self._live_cursor = (idx + 1) % len(clients)
                    return wav(pcm)
                log.warning("Gemini voice %s said %.1f s for about %.1f s of text: not sent", model, seconds, expected)
                break  # the model does not read faithfully today: the next one
            if slow:
                self._tts_resting[model] = time.monotonic() + GEMINI_TTS_REST_SECONDS
                log.warning("Gemini voice %s resting for %d s", model, GEMINI_TTS_REST_SECONDS)
        return None

    async def _live_once(self, client, model: str, config, text: str, expected: float) -> bytes:
        """One voice note from one key: the first sound within a few seconds, then the whole of it."""
        pcm = bytearray()
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_client_content(turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)
            messages = session.receive().__aiter__()
            deadline = time.monotonic() + LIVE_FIRST_AUDIO_SECONDS
            while True:
                message = await asyncio.wait_for(messages.__anext__(), timeout=max(0.1, deadline - time.monotonic()))
                content = message.server_content
                for part in (content.model_turn.parts if content and content.model_turn else None) or []:
                    if part.inline_data and part.inline_data.data:
                        if not pcm:  # it speaks: now the time to say the whole text
                            deadline = time.monotonic() + expected * 1.6 + 5
                        pcm.extend(part.inline_data.data)
                if content and content.turn_complete:
                    break
        if not pcm:
            raise TimeoutError("no audio")
        return bytes(pcm)

    async def _speak_gemini(self, text: str, language: str, mood: str = DEFAULT_MOOD) -> bytes | None:
        """Gemini TTS on all available API keys; returns WAV bytes or None on failure/quota."""
        if not self.llm._clients:
            return None
        voice_name = (self.voice_name or GEMINI_TTS_VOICES.get(language, "Aoede")).title()
        lang_label = LANG_LABELS.get(language, "English")
        prompt = (
            f"Say the following in {lang_label} like a close friend sending a WhatsApp voice note: "
            "relaxed, a natural conversational pace with small pauses and breathing, never flat or "
            f"announcer-like. Say it {MOODS.get(mood, MOODS[DEFAULT_MOOD])[0]}:\n\n" + text
        )
        # A longer text takes longer to say: a request given up too early still costs its quota.
        attempt_timeout = min(
            GEMINI_TTS_MAX_ATTEMPT_SECONDS,
            GEMINI_TTS_TIMEOUT + len(text) / CHARS_PER_SECOND * GEMINI_TTS_SECONDS_PER_SPOKEN_SECOND,
        )
        total_seconds = max(GEMINI_TTS_TOTAL_SECONDS, 1.5 * attempt_timeout + 10)
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
        )
        clients = self.llm._clients
        deadline = time.monotonic() + total_seconds
        for model in GEMINI_TTS_MODELS:
            if self._tts_resting.get(model, 0.0) > time.monotonic():
                continue
            slow = 0  # timeouts and server errors: the model is at fault, not one key
            for step in range(len(clients)):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                idx = (self._tts_cursor + step) % len(clients)
                if self._tts_key_resting.get((idx, model), 0.0) > time.monotonic():
                    continue
                if self._quota_day == quota_day() and (self._key_id(idx), model) in self._exhausted:
                    continue  # its quota for today is spent: not asked again until it renews
                try:
                    response = await asyncio.wait_for(
                        clients[idx].aio.models.generate_content(model=model, contents=prompt, config=config),
                        timeout=min(attempt_timeout, remaining),
                    )
                    if response.candidates:
                        pcm = response.candidates[0].content.parts[0].inline_data.data
                        if pcm:
                            self._tts_cursor = (idx + 1) % len(clients)
                            await self._count(idx, model)
                            return wav(pcm)
                except Exception as error:
                    if _over_quota(error):
                        log.info("Gemini TTS %s key %d over quota: next key", model, idx)
                        self._tts_key_resting[(idx, model)] = time.monotonic() + GEMINI_TTS_KEY_REST_SECONDS
                        await self._count(idx, model, exhausted=True)
                        continue  # this key's daily quota is spent: the next key may have some left
                    if isinstance(error, errors.APIError) and error.code in (401, 403) and hasattr(self.llm, "_disable_key"):
                        self.llm._disable_key(idx)  # a key Google refuses: out of the count and the rotation
                        continue
                    log.warning("Gemini TTS %s key %d failed: %s: %s", model, idx, type(error).__name__, error)
                    slow += 1
                    if slow >= GEMINI_TTS_SLOW_FAILURES:
                        break
            if slow:
                # Skip the model for a while instead of paying the same wait on every message
                # before the fallback voice.
                self._tts_resting[model] = time.monotonic() + GEMINI_TTS_REST_SECONDS
                log.warning("Gemini TTS %s resting for %d s", model, GEMINI_TTS_REST_SECONDS)
        return None

    async def _speak_edge(self, text: str, language: str, mood: str = DEFAULT_MOOD) -> bytes | None:
        """edge-tts fallback: free, no quota. Plain text only (see EDGE_VOICES)."""
        voice = EDGE_VOICES.get(language, EDGE_VOICES["en"])
        _, rate, pitch = MOODS.get(mood, MOODS[DEFAULT_MOOD])
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch)
            audio = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
            if audio:
                return bytes(audio)
            log.warning("edge-tts returned no audio (language=%s)", language)
        except Exception as error:
            log.error("edge-tts failed: %s: %s", type(error).__name__, error)
        return None

    async def script(self, text: str, language: str) -> str:
        """What Jeli says of a short written answer: the same, in spoken language and with feeling.
        The text as it is when the model cannot help, or when it lost a number or a date."""
        return (await self._rewrite(text, language))[0]

    async def _rewrite(self, text: str, language: str) -> tuple[str, str]:
        """The spoken version of an answer and its mood (see MOODS)."""
        if len(text) < 15 or len(text) > SPEAK_REWRITE_MAX_CHARS:
            return text, DEFAULT_MOOD
        label = LANG_LABELS.get(language, "English")
        # Said in the request, not only in the system: the light models follow the request.
        prompt = f"Say this answer as a voice note, entirely in {label}, without a list:\n\n{text}"
        mood = DEFAULT_MOOD
        written = set(_NUMBER.findall(text))
        for _ in range(REWRITE_ATTEMPTS):
            try:
                said = await self.llm.generate(prompt, Script, system=SPEAK_SYSTEM, timeout=12, temperature=0.7, attempts=2)
            except LLMUnavailable:
                return text, mood
            mood = said.mood.strip().lower() if said.mood.strip().lower() in MOODS else DEFAULT_MOOD
            result = " ".join(said.text.split())
            spoken_numbers = set(_NUMBER.findall(result))
            # A human summary may leave out who announced what and when (dates, numbers of their
            # own), not the facts: a number the answer does not have is never said, and a summary
            # that dropped nearly all of its numbers lost what was coming.
            if (
                not result
                or len(result) > len(text) * 1.6 + 80
                or not spoken_numbers <= written
                or len(spoken_numbers) < len(written) / 3
            ):
                log.info("Spoken rewrite rejected (empty, too long, a number added or nearly all lost): reading the answer as it is")
                return text, mood
            # The light models sometimes give the written answer back as it is: ask again.
            if result != " ".join(text.split()):
                return result, mood
            log.info("Spoken rewrite came back as the written answer: asking again")
        return text, mood

    async def speak(self, text: str, language: str = "") -> bytes | None:
        """The text read aloud; None when all voices fail. `language`: the one the understanding
        step chose for this reply (Reply.language); guessed from the words only when it is not given.
        Gemini first, on every key: Gemini TTS, then its native-audio voice; edge-tts only when no
        Gemini voice answers. Long replies are truncated at a sentence boundary: the full text is
        sent alongside."""
        text = text.strip()
        if not text:
            return None
        if len(text) > MAX_SPOKEN_CHARS:
            cutoff = text[:MAX_SPOKEN_CHARS].rfind(". ")
            text = text[:cutoff + 1] if cutoff > 300 else text[:MAX_SPOKEN_CHARS]
        # The reply's language, decided by the model with the whole message in view: word lists
        # mistake an answer mixing French and English names (a rewrite or a voice told the wrong
        # language translates, or mixes, the answer). Guessed from the words only as a last resort.
        spoken_language = language if language in LANG_LABELS else detect_language(text)
        said, mood = await self._rewrite(text, spoken_language)
        speech = for_speech(said)
        voices = {
            "natural": lambda: self._speak_gemini(speech, spoken_language, mood),
            "live": lambda: self._speak_live(speech, mood, spoken_language),
        }
        for name in VOICE_ENGINES.get(self.engine, VOICE_ENGINES["auto"]):
            audio = await voices[name]()
            if audio:
                return audio
        if self.engine != "backup":
            log.warning("No Gemini voice answered: the fallback voice speaks")
        return await self._speak_edge(speech, spoken_language, mood)
