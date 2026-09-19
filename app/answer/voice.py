"""Voice notes, as WhatsApp members use them: Jeli listens to a question asked by voice, and answers
by voice — when it was asked by voice, or when the member asks for a voice reply.

Listening: the voice note goes to the answer model as audio, which writes down what was said.
Speaking: a speech model reads the answer aloud (the words only: quotes, links and mentions stay in
the text that follows it); WAHA turns the audio into a WhatsApp voice note. When any step fails, the
member gets the written answer instead: a voice reply is a courtesy, never a condition.
"""

import asyncio
import io
import logging
import re
import wave

from google.genai import errors, types
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

SPEECH_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
VOICE_NAME = "Puck"  # lively and warm
SPEECH_TIMEOUT = 40
SAMPLE_RATE = 24_000  # the speech models return 16-bit mono PCM at 24 kHz
# A voice note says the answer in about a minute at most; longer answers stay written.
MAX_SPOKEN_CHARS = 900
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
SPEAK_STYLE = "Read this WhatsApp voice reply aloud, warmly and naturally, at a relaxed pace:\n\n{text}"


class Heard(BaseModel):
    text: str


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


class Voice:
    def __init__(self, llm: LLM, models: tuple[str, ...] = SPEECH_MODELS):
        self.llm = llm  # listens with the answer models; speaks with its client
        self.models = models

    async def listen(self, audio: bytes, mimetype: str) -> str:
        """What the member said; "" when nothing could be heard (or no model is available)."""
        if not audio or len(audio) > MAX_VOICE_BYTES:
            return ""
        part = types.Part(inline_data=types.Blob(mime_type=mimetype.split(";")[0].strip() or "audio/ogg", data=audio))
        try:
            heard = await self.llm.generate([part, "Transcribe this voice note."], Heard, system=LISTEN_SYSTEM, timeout=20, temperature=0, attempts=2)
        except LLMUnavailable:
            log.warning("Could not listen to a voice note: no model available")
            return ""
        return " ".join(heard.text.split())

    async def speak(self, text: str) -> bytes | None:
        """The text read aloud, as a WAV file; None when it is too long or no speech model answers."""
        text = text.strip()
        if not text or len(text) > MAX_SPOKEN_CHARS:
            return None
        config = types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE_NAME))
            ),
        )
        for model in self.models:
            try:
                response = await asyncio.wait_for(
                    self.llm.client.aio.models.generate_content(model=model, contents=SPEAK_STYLE.format(text=text), config=config),
                    timeout=SPEECH_TIMEOUT,
                )
                blob = response.candidates[0].content.parts[0].inline_data
                rate = re.search(r"rate=(\d+)", blob.mime_type or "")
                return wav(blob.data, int(rate.group(1)) if rate else SAMPLE_RATE)
            except (errors.APIError, TimeoutError, IndexError, AttributeError, TypeError) as error:
                log.warning("Speech model %s failed (%s), trying the next one", model, type(error).__name__)
        return None
