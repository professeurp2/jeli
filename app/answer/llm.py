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
# Measured in production (21 Sep): with 7 keys × 3 models, a bad moment made one answer try 21
# (key, model) pairs at 6 s each — p90 latency 22 s, worst 95 s. A call now tries at most this
# many pairs: the best model on two keys, then the next models. With 15+ keys the round-robin
# cursor spreads the load; a key that failed rests, so the next call starts elsewhere.
MAX_ATTEMPTS = 4
KEYS_PER_MODEL_FIRST = 2

Schema = TypeVar("Schema", bound=BaseModel)


class GeneratedAnswer(BaseModel):
    answered: bool
    answer: str
    # Ids of the excerpt lines that state the answer: "3.2" (excerpt 3, message 2), or "3".
    sources: list[str] = []
    # The answer comes from the background brief alone (a general question about the community).
    from_background: bool = False


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
            self._api_keys: list[str] = []
        else:
            keys = [api_keys] if isinstance(api_keys, str) else api_keys
            self._api_keys = [k for k in keys if k]
            self._clients = [genai.Client(api_key=k) for k in self._api_keys]
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
        """The (key, model) pairs to try, in order: the best model on a couple of keys, then each
        next model on a couple of keys, then the remaining keys of each model — keys rotating from
        self._next_key so that the load spreads over all of them. Pairs in cooldown are left out;
        only when every pair rests are they tried, the soonest to recover first (a per-minute 429
        may already be over)."""
        n = len(self._clients)
        now = self._clock()
        fresh: dict[str, list[tuple[int, str]]] = {model: [] for model in self.models}
        resting: list[tuple[float, int, str]] = []
        for model in self.models:
            for offset in range(n):
                ki = (self._next_key + offset) % n
                until = self._resting_until.get((ki, model), 0)
                if until <= now:
                    fresh[model].append((ki, model))
                else:
                    resting.append((until, ki, model))
        first = [pair for model in self.models for pair in fresh[model][:KEYS_PER_MODEL_FIRST]]
        rest = [pair for model in self.models for pair in fresh[model][KEYS_PER_MODEL_FIRST:]]
        if first or rest:
            return first + rest
        return [(ki, model) for _, ki, model in sorted(resting)]

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
        """`attempts`: how many (key, model) pairs to try at most (MAX_ATTEMPTS by default) — one
        for optional steps so that a busy model never doubles the wait."""
        if attempts is None:
            attempts = MAX_ATTEMPTS
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
