"""Voice notes, as WhatsApp members use them: Jeli listens to a question asked by voice, and answers
by voice — when it was asked by voice, or when the member asks for a voice reply.

Listening: the voice note goes to the answer model as audio, which writes down what was said.
Speaking: Gemini TTS (primary, more natural and emotional) → edge-tts (fallback, free, no quota).
The audio bytes returned by speak() are self-describing: RIFF header = WAV (from Gemini PCM),
no RIFF header = MP3 (from edge-tts). WAHA has convert:True so it transcodes to OGG/Opus anyway.
When any step fails, the member gets the written answer instead: a voice reply is a courtesy.
"""

import asyncio
import io
import logging
import re
import wave

import edge_tts
from google.genai import types
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

# Gemini TTS — primary TTS engine: more natural, emotionally expressive, instruction-following.
# Uses the same API key pool as the LLM (existing rotation in LLM._clients).
# Tried in order on every key (checked on the key on 21 Sep 2026: both exist; the free tier
# allows about 10 requests a day per key and model, hence the rotation and the edge-tts fallback).
GEMINI_TTS_MODELS = ["gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]
GEMINI_TTS_MODEL = GEMINI_TTS_MODELS[0]
GEMINI_TTS_VOICES = {"fr": "Aoede", "en": "Aoede"}  # warm, expressive multilingual voice
GEMINI_TTS_TIMEOUT = 20.0

# edge-tts — fallback when Gemini TTS quota is exhausted or unavailable.
# edge-tts (7.x) escapes what it is given and builds its own SSML: it must receive plain text,
# never SSML, or it reads the tags aloud ("Speak version 1.0 xmlns…").
EDGE_VOICES = {
    "fr": "fr-FR-DeniseNeural",
    "en": "en-US-AriaNeural",
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
EMOJI = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿️‍]")

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
    text = _BULLET.sub(", ", text)          # "- item" → ", item"
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
    text = LINK.sub("", text)
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

    async def _speak_gemini(self, text: str, language: str) -> bytes | None:
        """Gemini TTS on all available API keys; returns WAV bytes or None on failure/quota."""
        if not self.llm._clients:
            return None
        voice_name = (self.voice_name or GEMINI_TTS_VOICES.get(language, "Aoede")).title()
        _LANG_LABELS = {
            "fr": "French", "en": "English", "sw": "Swahili",
            "rw": "Kinyarwanda", "ln": "Lingala", "wo": "Wolof", "am": "Amharic",
        }
        lang_label = _LANG_LABELS.get(language, "English")
        prompt = (
            f"Read the following message in {lang_label} as a warm, friendly assistant "
            "who genuinely cares — conversational, natural and emotionally present:\n\n" + text
        )
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                )
            ),
        )
        for model in GEMINI_TTS_MODELS:
            for idx, client in enumerate(self.llm._clients):
                try:
                    response = await asyncio.wait_for(
                        client.aio.models.generate_content(model=model, contents=prompt, config=config),
                        timeout=GEMINI_TTS_TIMEOUT,
                    )
                    if response.candidates:
                        pcm = response.candidates[0].content.parts[0].inline_data.data
                        if pcm:
                            return wav(pcm)
                except Exception as error:
                    log.warning("Gemini TTS %s key %d failed: %s: %s", model, idx, type(error).__name__, error)
        return None

    async def _speak_edge(self, text: str, language: str) -> bytes | None:
        """edge-tts fallback: free, no quota. Plain text only (see EDGE_VOICES)."""
        voice = EDGE_VOICES.get(language, EDGE_VOICES["en"])
        try:
            communicate = edge_tts.Communicate(text, voice)
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

    async def speak(self, text: str, language: str = "en") -> bytes | None:
        """The text read aloud; None when all TTS engines fail.
        Tries Gemini TTS first (more natural, emotional), falls back to edge-tts on quota/error.
        Long replies are truncated at a sentence boundary: the full text is sent alongside."""
        text = text.strip()
        if not text:
            return None
        if len(text) > MAX_SPOKEN_CHARS:
            cutoff = text[:MAX_SPOKEN_CHARS].rfind(". ")
            text = text[:cutoff + 1] if cutoff > 300 else text[:MAX_SPOKEN_CHARS]
        speech = for_speech(text)
        audio = await self._speak_gemini(speech, language)
        if audio:
            return audio
        return await self._speak_edge(speech, language)
