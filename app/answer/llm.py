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
COOLDOWN_SECONDS = {"quota": 300, "unavailable": 60, "invalid": 86_400}

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
        self._next_key = 0  # round-robin cursor: advanced after each success
        self._clock = clock
        # Cooldown per (key_index, model_name) pair.
        self._resting_until: dict[tuple[int, str], float] = {}

    @property
    def client(self) -> genai.Client:
        return self._clients[self._next_key % len(self._clients)]

    def _rest(self, key: int, model: str, reason: str) -> None:
        self._resting_until[(key, model)] = self._clock() + COOLDOWN_SECONDS[reason]
        log.warning("Key %d model %s %s, skipped for %d s", key, model, reason, COOLDOWN_SECONDS[reason])

    def _disable_key(self, key: int) -> None:
        """Mark all models on a key as invalid for 24 h (e.g. after 401 UNAUTHENTICATED)."""
        until = self._clock() + COOLDOWN_SECONDS["invalid"]
        for m in self.models:
            self._resting_until[(key, m)] = until
        log.error("Key %d disabled for 24 h — verify it is valid and Gemini API is enabled on its project", key)

    def status(self) -> list[tuple[str, int]]:
        """Each model and minimum seconds it still rests across all keys (0: at least one key ready)."""
        now = self._clock()
        n = len(self._clients)
        return [
            (m, min(max(0, int(self._resting_until.get((ki, m), 0) - now)) for ki in range(n)))
            for m in self.models
        ]

    def _build_pairs(self) -> list[tuple[int, str]]:
        """Model-first, key-round-robin ordering.

        Tries the best model across all keys before falling back to the next model.
        Within each model, keys rotate from self._next_key for even quota distribution.
        Pairs still in cooldown are appended last as a last-resort fallback.
        """
        n = len(self._clients)
        now = self._clock()
        preferred: list[tuple[int, str]] = []
        fallback: list[tuple[int, str]] = []
        for model in self.models:
            for offset in range(n):
                ki = (self._next_key + offset) % n
                if self._resting_until.get((ki, model), 0) <= now:
                    preferred.append((ki, model))
                else:
                    fallback.append((ki, model))
        return preferred + fallback

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
        """`attempts`: how many (key, model) pairs to try at most (all by default) — one for optional
        steps so that a busy model never doubles the wait."""
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            media_resolution=media_resolution,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        tried: set[tuple[int, str]] = set()
        for key_idx, model in self._build_pairs()[:attempts]:
            if (key_idx, model) in tried:
                continue
            tried.add((key_idx, model))
            client = self._clients[key_idx]
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents, config=config),
                    timeout=timeout,
                )
                self._next_key = (key_idx + 1) % len(self._clients)  # advance round-robin
                parsed = response.parsed
                return parsed if isinstance(parsed, schema) else schema.model_validate_json(response.text)
            except errors.ClientError as error:
                if error.code == 401:
                    self._disable_key(key_idx)  # bad key: skip all models on it for 24 h
                elif error.code in (404, 429):
                    self._rest(key_idx, model, "quota")
                else:
                    raise
            except (errors.ServerError, TimeoutError, httpx.TransportError):
                self._rest(key_idx, model, "unavailable")
            except (ValidationError, ValueError) as error:
                log.warning("Key %d model %s returned unusable output (%s), trying next", key_idx, model, type(error).__name__)
        raise LLMUnavailable

    async def answer(self, system: str, prompt: str) -> GeneratedAnswer:
        return await self.generate(prompt, GeneratedAnswer, system=system)
