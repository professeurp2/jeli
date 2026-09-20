"""Gemini calls with structured output, falling back from one model to the next.

Used for answers (fast, a few seconds) and for transcription (long, one call per recording window).
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

# Per model: the whole answer must stay under 10 s, and a slow model should not block the fallback.
TIMEOUT_SECONDS = 6
# Gemini 3 models think before answering. Measured on a real question (2,300-token prompt):
# default 14 s or overloaded, "low" 5.7 s and wrongly "not found", "minimal" 1.7 s and correct.
THINKING_LEVEL = "minimal"
# A model out of quota is skipped for a while instead of costing a failed round trip per question.
COOLDOWN_SECONDS = {"quota": 300, "unavailable": 60}

Schema = TypeVar("Schema", bound=BaseModel)


class GeneratedAnswer(BaseModel):
    answered: bool
    answer: str
    sources: list[int]


class LLMUnavailable(Exception):
    """Every model failed: quota, overload, timeout or unusable output."""


class LLM:
    def __init__(
        self,
        api_keys: list[str] | str,
        models: list[str],
        client: genai.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not models:
            raise ValueError("At least one model is required")
        self.models = models
        if client:
            self._clients = [client]
        else:
            keys = [api_keys] if isinstance(api_keys, str) else api_keys
            self._clients = [genai.Client(api_key=k) for k in keys if k]
        self._key = 0  # index of the active key
        self._clock = clock
        # Cooldown per (key_index, model_name) pair.
        self._resting_until: dict[tuple[int, str], float] = {}

    @property
    def client(self) -> genai.Client:
        return self._clients[self._key]

    def _rest(self, key: int, model: str, reason: str) -> None:
        self._resting_until[(key, model)] = self._clock() + COOLDOWN_SECONDS[reason]
        log.warning("Key %d model %s %s, skipped for %d s", key, model, reason, COOLDOWN_SECONDS[reason])

    def status(self) -> list[tuple[str, int]]:
        """Each model and the seconds it still rests on the active key (0: available)."""
        now = self._clock()
        return [(m, max(0, int(self._resting_until.get((self._key, m), 0) - now))) for m in self.models]

    def _available(self, key: int) -> list[str]:
        now = self._clock()
        ready = [m for m in self.models if self._resting_until.get((key, m), 0) <= now]
        return ready or self.models  # all resting: try them anyway rather than not answering

    async def generate(
        self,
        contents: Any,
        schema: type[Schema],
        system: str | None = None,
        timeout: float = TIMEOUT_SECONDS,
        temperature: float = 0.2,
        media_resolution: types.MediaResolution | None = None,
        attempts: int | None = None,
    ) -> Schema:
        """`attempts`: how many models to try at most (all by default) — one for optional steps,
        so that a busy model never doubles the wait."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            media_resolution=media_resolution,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # Try every (key, model) pair: models on the active key first, then rotate keys.
        tried: list[tuple[int, str]] = []
        all_pairs = [(ki, m) for ki in range(len(self._clients)) for m in self._available(ki)]
        # Prioritise active key; bring it to the front without duplicating.
        active_models = [(self._key, m) for m in self._available(self._key)]
        other_models = [(ki, m) for ki, m in all_pairs if ki != self._key]
        for key_idx, model in (active_models + other_models)[:attempts]:
            if (key_idx, model) in tried:
                continue
            tried.append((key_idx, model))
            client = self._clients[key_idx]
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents, config=config),
                    timeout=timeout,
                )
                self._key = key_idx  # remember which key last worked
                parsed = response.parsed
                return parsed if isinstance(parsed, schema) else schema.model_validate_json(response.text)
            except errors.ClientError as error:
                # Quota (429) and retired models (404) move on to the next model/key.
                if error.code not in (404, 429):
                    raise
                self._rest(key_idx, model, "quota")
            except (errors.ServerError, TimeoutError, httpx.TransportError):
                self._rest(key_idx, model, "unavailable")
            except (ValidationError, ValueError) as error:
                log.warning("Key %d model %s returned unusable output (%s), trying next", key_idx, model, type(error).__name__)
        raise LLMUnavailable

    async def answer(self, system: str, prompt: str) -> GeneratedAnswer:
        return await self.generate(prompt, GeneratedAnswer, system=system)
