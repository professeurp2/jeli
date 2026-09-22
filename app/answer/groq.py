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
# A failing model (rate limit, overload) is left alone for a while rather than costing every call.
REST_SECONDS = 120
# The spare engine must not make the member wait longer than Gemini already has.
TIMEOUT_SECONDS = 8

Schema = TypeVar("Schema", bound=BaseModel)


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
        if self.available:
            log.info("Groq: spare engine ready, models %s", ", ".join(self.models))

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.models)

    def _ready(self) -> list[str]:
        now = self._clock()
        fresh = [m for m in self.models if self._resting_until.get(m, 0) <= now]
        return fresh or self.models[:1]  # all resting: try the first again rather than give up

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
