"""Groq, the spare engine: used only when every Gemini model has refused.

Measured on 22 Sep at 16:48 and again at 17:00: every Gemini model answered "high demand" (503) on
every key, and Jeli told a member it could not summarise anything. Paying for Gemini fixes the
priority, not an outage at Google. Groq is a second company, a second network and a generous free
tier, so the two are unlikely to be down at the same minute.

It answers text only: the calls that carry a recording, a picture or a sticker stay on Gemini.
"""

import json
import logging
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

URL = "https://api.groq.com/openai/v1/chat/completions"
# Listening is a separate model: Whisper, hosted by Groq. It takes over when no Gemini model can
# hear a voice note, so a member who speaks still gets an answer.
HEAR_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
HEAR_MODEL = "whisper-large-v3-turbo"
# Groq retires model ids without notice (measured 22 Sep: llama-3.3-70b-versatile answered 404 on
# a fresh key). Rather than trust a name written months earlier, Jeli asks the key what it has and
# keeps the best of it, in this order. A name is matched as a prefix, so dated variants count.
LIST_URL = "https://api.groq.com/openai/v1/models"
PREFERRED = ("llama-3.3-70b", "llama-3.1-70b", "kimi-k2", "gpt-oss-120b", "qwen3", "gpt-oss-20b", "llama-3.1-8b")
# The families that hold a conversation. An allow-list, not a block-list: a key also carries models
# that listen, speak or moderate, and their names say nothing reliable (measured 22 Sep: the first
# pick was "canopylabs/orpheus-arabic-saudi", a voice). Whatever is chosen is then tried before it
# is trusted, so an unknown name never becomes Jeli's spare engine on the strength of its spelling.
CHAT_FAMILIES = ("llama", "qwen", "kimi", "gpt-oss", "mixtral", "mistral", "gemma", "deepseek")
NOT_FOR_ANSWERS = ("whisper", "tts", "guard", "embed", "prompt-", "orpheus", "playai", "moderation")
# Models tried at startup before the spare engine is declared ready.
MODELS_KEPT = 2
# A failing model (rate limit, overload) is left alone for a while rather than costing every call.
REST_SECONDS = 120
# The spare engine must not make the member wait longer than Gemini already has.
TIMEOUT_SECONDS = 8
# Gemini reads a million tokens; a free model does not. Rather than guess each model's limit — it
# differs per model and changes without notice — the source is sent, and shortened only when the
# server says it is too big. This ceiling is just a sanity bound, so a runaway prompt is not posted.
MAX_PROMPT_CHARS = 200_000
SHRINK_ROUNDS = 4
SHRINK_FACTOR = 0.5
# How the server says "too big", whatever the wording it uses that day. Only consulted for a prompt
# that could plausibly be too big: measured 23 Sep at 09:09, a 40-character test question was
# shortened five times because a model's refusal happened to contain one of these words. A short
# prompt refused is a refusal, not a size.
TOO_BIG_WORDS = ("too large", "too long", "context_length", "context length", "maximum context", "reduce the length")
SHRINK_FLOOR = 4_000
# The head holds the instructions and the oldest context; the tail holds what is most recent, which
# is what members ask about. The middle is what goes.
KEEP_HEAD = 0.3
OMITTED = "\n\n[… {n:,} characters from the middle are missing: this is only part of the source …]\n\n"
# Said to the model when it reads only part, so the answer admits it. Whatever the question was —
# a catch-up, a recap, a document — the honesty travels with the answer instead of being bolted on
# by each caller.
TOLD_PART = (
    "\n\nIMPORTANT: you were given only part of the source; its middle was left out. Answer with "
    "what you have, and say so plainly in one short sentence, in the language you are answering in."
)

Schema = TypeVar("Schema", bound=BaseModel)


def _as_text(contents: Any) -> str | None:
    """The prompt as plain text, or None when it carries something Groq cannot read.

    Gemini takes a list of parts, and several of Jeli's steps build one even when every part is
    text (the emotions, for instance, send ["Message: …"]). Refusing those would quietly cost the
    spare engine a feature per caller — measured 23 Sep: "not a text prompt" on every message."""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, (list, tuple)) and contents and all(isinstance(part, str) for part in contents):
        return "\n\n".join(contents)
    return None


def _shorten(text: str, budget: int) -> str:
    """`text` cut down to `budget` characters, keeping its beginning and its end."""
    if len(text) <= budget or budget <= 0:
        return text
    # The note about the gap counts against the budget: the result really is `budget` long, so a
    # halving is a halving and the shrinking cannot stall (measured: it added 88 characters back).
    room = max(1, budget - len(OMITTED.format(n=len(text))))
    head = int(room * KEEP_HEAD)
    kept = text[:head] + OMITTED.format(n=len(text) - room) + text[len(text) - (room - head):]
    return kept[:budget] if len(kept) > budget else kept


class _Alive(BaseModel):
    """The shape of the startup check's answer (see Groq.check)."""

    answered: bool
    answer: str


class BackupUnavailable(Exception):
    """The spare engine has no key, cannot answer this kind of call, or failed too."""


class Groq:
    """Structured answers from Groq's OpenAI-compatible endpoint, one model after the other."""

    def __init__(self, api_key: str, models: list[str], clock=None, client: httpx.AsyncClient | None = None):
        import time

        self.api_key = api_key.strip()
        self.models = [m for m in models if m]
        self._clock = clock or time.monotonic
        self._client = client
        self._resting_until: dict[str, float] = {}
        self.used = 0  # answers this engine has rescued, since the start
        self.heard = 0  # voice notes it has listened to when Gemini could not
        # What a first call proved, for the dashboard: "" not tried, "ok", or why it failed.
        self.checked = ""
        self.hear_model = HEAR_MODEL  # confirmed against the key by check()
        self.on_key: list[str] = []  # every model id the key can see, as check() last read it
        if self.available:
            log.info("Groq: spare engine ready, models %s", ", ".join(self.models))

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.models)

    def _ready(self) -> list[str]:
        now = self._clock()
        fresh = [m for m in self.models if self._resting_until.get(m, 0) <= now]
        return fresh or self.models[:1]  # all resting: try the first again rather than give up

    async def models_on_key(self) -> list[str]:
        """The model ids this key can actually use, newest listing from Groq ([] when unreachable)."""
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.get(LIST_URL, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=15)
            if response.status_code >= 400:
                log.warning("Groq would not list its models (%d)", response.status_code)
                return []
            return [str(m.get("id", "")) for m in response.json().get("data", []) if m.get("id")]
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            log.warning("Groq would not list its models (%s)", type(error).__name__)
            return []
        finally:
            if self._client is None:
                await client.aclose()

    def _pick(self, available: list[str]) -> list[str]:
        """The models worth trying: the configured ones when the key has them, otherwise the
        conversation models it does have, best first."""
        usable = [m for m in available if not any(bad in m for bad in NOT_FOR_ANSWERS)]
        kept = [m for m in self.models if m in usable]
        if kept:
            return kept
        chat = [m for m in usable if any(family in m for family in CHAT_FAMILIES)]
        best = [m for want in PREFERRED for m in chat if m.split("/")[-1].startswith(want)]
        return list(dict.fromkeys(best + chat))

    async def check(self) -> str:
        """One cheap call at startup, so the team sees on the dashboard whether the spare engine
        really answers — rather than finding out the key is wrong the day Gemini goes down. The
        model list is refreshed first: a name that no longer exists is replaced, not endured."""
        if not self.available:
            self.checked = ""
            return ""
        available = await self.models_on_key()
        self.on_key = available
        if available:
            chosen = self._pick(available)
            whisper = [m for m in available if "whisper" in m]
            if whisper and self.hear_model not in whisper:
                self.hear_model = next((m for m in whisper if "turbo" in m), whisper[0])
                log.info("Groq: voice notes will be heard by %s", self.hear_model)
            if chosen != self.models:
                log.warning("Groq: models %s -> %s (what the key really has)", ", ".join(self.models), ", ".join(chosen))
                self.models = chosen
                self._resting_until.clear()
        # Candidates are tried, not trusted: whichever answer the test question become the spare
        # engine, in the order they were preferred. The rest are dropped, whatever they are called.
        candidates, working, failure = list(self.models), [], None
        for model in candidates[: MODELS_KEPT + 2]:
            self.models, self._resting_until = [model], {}
            try:
                await self.generate("Reply with {\"answered\": true, \"answer\": \"ok\"}.", _Alive, timeout=20)
                working.append(model)
                log.info("Groq: %s answered the test question", model)
            except Exception as error:  # a check must never prevent the start
                failure = error
                log.warning("Groq: %s did not answer the test question (%s)", model, error)
            if len(working) >= MODELS_KEPT:
                break
        self.models, self._resting_until = working or candidates, {}
        self.used = 0  # the test calls are not answers it rescued
        if working:
            self.checked = "ok"
            log.info("Groq: spare engine ready, models %s", ", ".join(working))
        else:
            self.checked = type(failure).__name__ if failure else "no model"
            log.error("Groq: the spare engine answered nothing — check GROQ_API_KEY and GROQ_MODELS")
        return self.checked

    async def hear(self, audio: bytes, filename: str = "voice.ogg", timeout: float = 30) -> str | None:
        """What a voice note says, heard by Whisper. None when it could not listen."""
        if not self.api_key or not audio:
            return None
        client = self._client or httpx.AsyncClient()
        try:
            response = await client.post(
                HEAR_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, audio)},
                data={"model": self.hear_model, "response_format": "json"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                log.warning("Groq could not listen (%d)", response.status_code)
                return None
            self.heard += 1
            return " ".join(str(response.json().get("text", "")).split())
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            log.warning("Groq could not listen (%s)", type(error).__name__)
            return None
        finally:
            if self._client is None:
                await client.aclose()

    def health(self) -> list[dict]:
        now = self._clock()
        return [{"model": m, "resting": max(0, int(self._resting_until.get(m, 0) - now))} for m in self.models]

    @staticmethod
    def _instructions(schema: type[Schema], system: str | None) -> str:
        """Groq has no response schema: the shape is asked for in words, then checked on the way back."""
        shape = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        return (system or "") + (
            "\n\nAnswer with a single JSON object, nothing else — no explanation, no code fence. "
            f"It must follow this JSON schema exactly, including every required field:\n{shape}"
        )

    async def _ask(self, client, model, text, schema, system, temperature, timeout, partial):
        """One call. Returns ("ok", answer), ("too big", None) or ("failed", None)."""
        instructions = self._instructions(schema, system) + (TOLD_PART if partial else "")
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": text},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        try:
            response = await client.post(
                URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"}, timeout=timeout
            )
        except (httpx.HTTPError, TimeoutError) as error:
            log.warning("Groq %s failed (%s), resting", model, type(error).__name__)
            self._resting_until[model] = self._clock() + REST_SECONDS
            return "failed", None
        if response.status_code >= 400:
            said = response.text[:300]
            too_big = response.status_code == 413 or any(word in said.lower() for word in TOO_BIG_WORDS)
            if too_big and len(text) > SHRINK_FLOOR:
                log.info("Groq %s: this source is more than it takes, shortening", model)
                return "too big", None  # the model is fine: the prompt was not
            log.warning("Groq %s refused (%d): %s", model, response.status_code, said)
            self._resting_until[model] = self._clock() + REST_SECONDS
            return "failed", None
        try:
            answer = schema.model_validate_json(response.json()["choices"][0]["message"]["content"])
        except (ValidationError, ValueError, KeyError, IndexError) as error:
            log.warning("Groq %s returned unusable output (%s), resting", model, type(error).__name__)
            self._resting_until[model] = self._clock() + REST_SECONDS
            return "failed", None
        self._resting_until[model] = 0.0
        return "ok", answer

    async def generate(
        self,
        contents: Any,
        schema: type[Schema],
        system: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        temperature: float = 0.2,
    ) -> Schema:
        """An answer, whatever the size of what it is asked to read.

        Gemini reads a million tokens; a free model does not, and every part of Jeli that hands it
        a long source — a day of messages, a session transcript, a document — would otherwise fail
        the same way. So the source is shortened until it fits, keeping the beginning and the end,
        and the model is told it is reading only part of it and asked to say so in its answer.
        Nothing here knows what a catch-up or a recap is: it works for all of them at once.
        """
        if not self.available:
            raise BackupUnavailable("no Groq key")
        text = _as_text(contents)
        if text is None:
            # Recordings, pictures and stickers: Groq is not given them.
            raise BackupUnavailable("not a text prompt")
        client = self._client or httpx.AsyncClient()
        partial = False
        if len(text) > MAX_PROMPT_CHARS:  # a ceiling, before the server is even asked
            text, partial = _shorten(text, MAX_PROMPT_CHARS), True
        try:
            for _ in range(SHRINK_ROUNDS + 1):
                too_big = False
                for model in self._ready():
                    outcome, answer = await self._ask(
                        client, model, text, schema, system, temperature, timeout, partial
                    )
                    if outcome == "ok":
                        self.used += 1
                        log.info(
                            "Groq %s answered where Gemini could not (%d times so far)%s",
                            model, self.used, ", from part of the source" if partial else "",
                        )
                        return answer
                    if outcome == "too big":
                        too_big = True
                        break
                if not too_big:
                    break  # the models failed for their own reasons: shortening would not help
                shorter = _shorten(text, int(len(text) * SHRINK_FACTOR))
                if len(shorter) >= len(text):
                    break
                text, partial = shorter, True
        finally:
            if self._client is None:
                await client.aclose()
        raise BackupUnavailable("every Groq model failed")
