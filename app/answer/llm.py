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
        api_key: str,
        models: list[str],
        client: genai.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not models:
            raise ValueError("At least one model is required")
        self.models = models
        self._client = client or genai.Client(api_key=api_key)
        self._clock = clock
        self._resting_until: dict[str, float] = {}

    @property
    def client(self) -> genai.Client:
        return self._client

    def _rest(self, model: str, reason: str) -> None:
        self._resting_until[model] = self._clock() + COOLDOWN_SECONDS[reason]
        log.warning("Model %s %s, skipped for %d s", model, reason, COOLDOWN_SECONDS[reason])

    def status(self) -> list[tuple[str, int]]:
        """Each model and the seconds it still rests after a failure (0: available)."""
        now = self._clock()
        return [(model, max(0, int(self._resting_until.get(model, 0) - now))) for model in self.models]

    def _available(self) -> list[str]:
        now = self._clock()
        ready = [m for m in self.models if self._resting_until.get(m, 0) <= now]
        # If every model is resting, try them all anyway rather than not answering.
        return ready or self.models

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
        for model in self._available()[:attempts]:
            try:
                response = await asyncio.wait_for(
                    self._client.aio.models.generate_content(model=model, contents=contents, config=config),
                    timeout=timeout,
                )
                parsed = response.parsed
                return parsed if isinstance(parsed, schema) else schema.model_validate_json(response.text)
            except errors.ClientError as error:
                # Quota (429) and retired models (404) move on to the next model; other client errors are bugs.
                if error.code not in (404, 429):
                    raise
                self._rest(model, "quota")
            except (errors.ServerError, TimeoutError, httpx.TransportError):
                # Overload, slowness or a network drop ("Server disconnected without sending a response").
                self._rest(model, "unavailable")
            except (ValidationError, ValueError) as error:
                log.warning("Model %s returned unusable output (%s), trying the next one", model, type(error).__name__)
        raise LLMUnavailable

    async def answer(self, system: str, prompt: str) -> GeneratedAnswer:
        return await self.generate(prompt, GeneratedAnswer, system=system)
