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

Schema = TypeVar("Schema", bound=BaseModel)


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

    async def generate(
        self,
        contents: Any,
        schema: type[Schema],
        system: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        temperature: float = 0.2,
    ) -> Schema:
        if not self.available:
            raise BackupUnavailable("no Groq key")
        if not isinstance(contents, str):
            # Recordings, pictures and stickers: Groq is not given them.
            raise BackupUnavailable("not a text prompt")
        body = {
            "messages": [
                {"role": "system", "content": self._instructions(schema, system)},
                {"role": "user", "content": contents},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        client = self._client or httpx.AsyncClient()
        try:
            for model in self._ready():
                try:
                    response = await client.post(URL, json={**body, "model": model}, headers=headers, timeout=timeout)
                    if response.status_code >= 400:
                        log.warning("Groq %s refused (%d), resting", model, response.status_code)
                        self._resting_until[model] = self._clock() + REST_SECONDS
                        continue
                    text = response.json()["choices"][0]["message"]["content"]
                    result = schema.model_validate_json(text)
                    self.used += 1
                    self._resting_until[model] = 0.0
                    log.info("Groq %s answered where Gemini could not (%d times so far)", model, self.used)
                    return result
                except (httpx.HTTPError, TimeoutError, ValidationError, ValueError, KeyError, IndexError) as error:
                    log.warning("Groq %s failed (%s), resting", model, type(error).__name__)
                    self._resting_until[model] = self._clock() + REST_SECONDS
        finally:
            if self._client is None:
                await client.aclose()
        raise BackupUnavailable("every Groq model failed")
