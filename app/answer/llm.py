"""Gemini calls with structured output, falling back from one model to the next.

Used for answers (fast, a few seconds) and for transcription (long, one call per recording window).
"""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, ValidationError

from app.answer.groq import BackupUnavailable, Groq

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
# When a spare engine is ready and the prompt is plain text, Gemini gets fewer slow failures before
# handing over: measured in the audit of 22 Sep, four overloaded models cost the member about 24 s
# of waiting before Groq was even asked. Two is enough to tell an outage from a slow model.
ATTEMPTS_BEFORE_BACKUP = 2
# A model that fails slowly (503 "high demand", a timeout, unusable output) does so on every key:
# it rests for everyone, longer at each failure in a row (1, 2, 4… minutes, at most 15), and the
# healthy models move ahead of it. Measured 22 Sep: with a fixed order, every answer paid a 6 s
# timeout on an overloaded Lite model before reaching the one that worked (median 16 s).
MODEL_REST_SECONDS = 60
MODEL_REST_MAX_SECONDS = 900
# Models that reject or slow down with a thinking level (measured 22 Sep: gemini-3-flash-preview
# answered in 4.6 s without one, 7.7 s with "minimal").
NO_THINKING_LEVEL = ("gemini-3-flash-preview", "gemini-3.7-flash", "gemini-3.8-flash", "gemini-2.")

Schema = TypeVar("Schema", bound=BaseModel)


class GeneratedAnswer(BaseModel):
    answered: bool
    answer: str
    # Ids of the excerpt lines that state the answer: "3.2" (excerpt 3, message 2), or "3".
    sources: list[str] = []
    # The answer comes from the background brief alone (a general question about the community).
    from_background: bool = False


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


def seconds_until_quota_renewal(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    return max(60.0, (quota_renewal(now) - now).total_seconds())


class LLMUnavailable(Exception):
    """Every model failed: quota, overload, timeout or unusable output."""


class LLM:
    def __init__(
        self,
        api_keys: list[str] | str,
        models: list[str],
        client: genai.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
        backup: Groq | None = None,
    ):
        if not models:
            raise ValueError("At least one model is required")
        self.models = models
        # The spare engine, another company: tried for text answers when Gemini has nothing left.
        self.backup = backup
        if client:
            self._clients = [client]
            self._api_keys: list[str] = []
        else:
            keys = [api_keys] if isinstance(api_keys, str) else api_keys
            self._api_keys = [k for k in keys if k]
            self._clients = [genai.Client(api_key=k) for k in self._api_keys]
        self._next_key = 0  # round-robin cursor: advanced after each success
        self._clock = clock
        self._wall_clock = lambda: datetime.now(timezone.utc)  # for daily quotas (tests may set it)
        # Cooldown per (key_index, model_name) pair.
        self._resting_until: dict[tuple[int, str], float] = {}
        # Keys Google refused (401): left out of every count until the 24 h are over.
        self._invalid_until: dict[int, float] = {}
        # Health per model, whatever the key: when it may be tried again, and its failures in a row.
        self._model_until: dict[str, float] = {}
        self._model_streak: dict[str, int] = {}
        # Latest measured latency per model (seconds), for the dashboard and the logs.
        self.latency: dict[str, float] = {}
        if self._api_keys:
            log.info("Gemini: %d API keys in rotation, models %s", len(self._api_keys), ", ".join(models))

    @property
    def key_count(self) -> int:
        return len(self._clients)

    def _model_failed(self, model: str) -> None:
        streak = self._model_streak.get(model, 0) + 1
        self._model_streak[model] = streak
        seconds = min(MODEL_REST_SECONDS * 2 ** (streak - 1), MODEL_REST_MAX_SECONDS)
        self._model_until[model] = self._clock() + seconds
        log.warning("Model %s failing (%d in a row): behind the others for %d s", model, streak, seconds)

    def _model_succeeded(self, model: str, seconds: float) -> None:
        self._model_streak[model] = 0
        self._model_until[model] = 0.0
        self.latency[model] = seconds

    def model_health(self) -> list[dict]:
        """Per model: failures in a row, seconds of rest left, latest latency — for the dashboard."""
        now = self._clock()
        return [
            {
                "model": m,
                "streak": self._model_streak.get(m, 0),
                "resting": max(0, int(self._model_until.get(m, 0) - now)),
                "latency": self.latency.get(m),
            }
            for m in self.models
        ]

    def _healthy_order(self) -> list[str]:
        """The configured order, with the models still resting behind those that work. A model whose
        rest is over is tried again in its place: if it still fails, it rests twice as long."""
        now = self._clock()
        return sorted(self.models, key=lambda m: (self._model_until.get(m, 0) > now, self.models.index(m)))

    @property
    def client(self) -> genai.Client | None:
        """None without a single Gemini key: Jeli then runs on the spare engine alone."""
        if not self._clients:
            return None
        return self._clients[self._next_key % len(self._clients)]

    def _rest(self, key: int, model: str, reason: str, seconds: float | None = None) -> None:
        seconds = COOLDOWN_SECONDS[reason] if seconds is None else seconds
        self._resting_until[(key, model)] = self._clock() + seconds
        log.warning("Key %d model %s %s, skipped for %d s", key, model, reason, seconds)

    def _disable_key(self, key: int) -> None:
        """Mark all models on a key as invalid for 24 h (e.g. after 401 UNAUTHENTICATED)."""
        until = self._clock() + COOLDOWN_SECONDS["invalid"]
        for m in self.models:
            self._resting_until[(key, m)] = until
        self._invalid_until[key] = until
        log.error("Key %d disabled for 24 h — verify it is valid and Gemini API is enabled on its project", key)

    def with_models(self, models: list[str]) -> "LLM":
        """The same keys on other models: rotation cursor apart, but sharing which (key, model) pairs
        rest and which keys Google refused — a key found spent here is spent everywhere."""
        if not models:
            raise ValueError("At least one model is required")
        view = object.__new__(LLM)
        view.__dict__.update(self.__dict__)
        view.models = list(models)
        view._next_key = 0
        return view

    def valid_keys(self) -> list[int]:
        """The keys Google accepts: all of them but those refused in the last 24 h."""
        now = self._clock()
        return [i for i in range(len(self._clients)) if self._invalid_until.get(i, 0) <= now]

    def status(self) -> list[tuple[str, int]]:
        """Each model and minimum seconds it still rests across all keys (0: at least one key ready)."""
        now = self._clock()
        n = len(self._clients)
        if not n:  # no Gemini key: every model is out, the spare engine carries the answers
            return [(m, int(COOLDOWN_SECONDS["quota"])) for m in self.models]
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
        order = self._healthy_order()
        fresh: dict[str, list[tuple[int, str]]] = {model: [] for model in order}
        resting: list[tuple[float, int, str]] = []
        for model in order:
            for offset in range(n):
                ki = (self._next_key + offset) % n
                until = max(self._resting_until.get((ki, model), 0), self._model_until.get(model, 0))
                if until <= now:
                    fresh[model].append((ki, model))
                else:
                    resting.append((until, ki, model))
        first = [pair for model in order for pair in fresh[model][:KEYS_PER_MODEL_FIRST]]
        rest = [pair for model in order for pair in fresh[model][KEYS_PER_MODEL_FIRST:]]
        if first or rest:
            return first + rest
        return [(ki, model) for _, ki, model in sorted(resting)]

    @staticmethod
    def _thinking(model: str) -> types.ThinkingConfig | None:
        return None if model.startswith(NO_THINKING_LEVEL) else types.ThinkingConfig(thinking_level=THINKING_LEVEL)

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
        spare = self.backup is not None and self.backup.available and isinstance(contents, str)
        if spare:
            attempts = min(attempts, ATTEMPTS_BEFORE_BACKUP)
        def config_for(model: str) -> types.GenerateContentConfig:
            return types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
                media_resolution=media_resolution,
                thinking_config=self._thinking(model),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            )

        tried: set[tuple[int, str]] = set()
        # Slow failures (overload, timeout, unusable output) count against `attempts`: they cost the
        # member's wait. A key refused or out of quota answers at once (a fraction of a second): the
        # next pair is tried without counting, or a model spent everywhere would fail every call
        # while the next model was free (measured 22 Sep: gemini-3.6-flash's daily quota gone on
        # every key, and each call gave up after two refusals without reaching the lite models).
        slow = 0
        pairs = self._build_pairs()
        position = 0
        while position < len(pairs) and slow < attempts:
            key_idx, model = pairs[position]
            position += 1
            if (key_idx, model) in tried:
                continue
            tried.add((key_idx, model))
            client = self._clients[key_idx]
            started = self._clock()
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(model=model, contents=contents, config=config_for(model)),
                    timeout=timeout,
                )
                self._next_key = (key_idx + 1) % len(self._clients)  # advance round-robin
                parsed = response.parsed
                result = parsed if isinstance(parsed, schema) else schema.model_validate_json(response.text)
                self._model_succeeded(model, self._clock() - started)
                return result
            except errors.ClientError as error:
                if error.code == 401:
                    self._disable_key(key_idx)  # bad key: skip all models on it for 24 h
                elif error.code == 429 and "PerDay" in str(error):
                    # The day's quota is spent: nothing to try again before it renews.
                    self._rest(key_idx, model, "quota", seconds=seconds_until_quota_renewal(self._wall_clock()))
                elif error.code in (404, 429):
                    self._rest(key_idx, model, "quota")
                else:
                    raise
                if error.code == 429:
                    # A quota is per key: the same model on the next key may still have some —
                    # tried next, before a lighter model (measured 22 Sep: the best model spent on
                    # two keys, still free on others, while the lite ones were overloaded).
                    later = next((i for i in range(position, len(pairs)) if pairs[i][1] == model), None)
                    if later is not None:
                        pairs.insert(position, pairs.pop(later))
            except (errors.ServerError, TimeoutError, httpx.TransportError) as error:
                log.warning(
                    "Key %d model %s unavailable after %.1f s (%s)", key_idx, model, self._clock() - started, type(error).__name__
                )
                self._model_failed(model)
                slow += 1
                # An overloaded or slow model is so on every key ("high demand" is Google's, not the
                # key's): the next model is tried at once rather than the same one elsewhere.
                pairs[position:] = [pair for pair in pairs[position:] if pair[1] != model]
            except (ValidationError, ValueError) as error:
                log.warning("Key %d model %s returned unusable output (%s), trying next", key_idx, model, type(error).__name__)
                self._model_failed(model)
                slow += 1
                pairs[position:] = [pair for pair in pairs[position:] if pair[1] != model]
        # Gemini has nothing left for this call: the spare engine answers rather than the member
        # being told to come back later. Text only — a recording or a picture it cannot take.
        if self.backup is not None and self.backup.available:
            try:
                return await self.backup.generate(contents, schema, system=system, temperature=temperature)
            except BackupUnavailable as error:
                log.warning("Spare engine could not answer either (%s)", error)
        raise LLMUnavailable

    async def answer(self, system: str, prompt: str) -> GeneratedAnswer:
        return await self.generate(prompt, GeneratedAnswer, system=system)
