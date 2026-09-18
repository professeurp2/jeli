"""Human-like pacing for the unofficial WhatsApp channel.

WhatsApp restricts accounts that behave like machines: instant replies, bursts, non-stop volume.
These helpers keep Jeli's rhythm close to a person's, following WAHA's guidance:
https://waha.devlike.pro/docs/overview/how-to-avoid-blocking/
"""

import asyncio
import math
import random
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

READING_SECONDS = (0.5, 1.5)
# Typing time grows with the answer's length, within these bounds, to stay under the 10 s target.
TYPING_SECONDS = (1.5, 5.0)
TYPING_CHARS_PER_SECOND = 60


def reading_delay() -> float:
    """Pause before marking a message as read: nobody reads a message the instant it arrives."""
    return random.uniform(*READING_SECONDS)


def typing_duration(text: str) -> float:
    """How long the "typing…" indicator shows before an answer of this length is sent."""
    low, high = TYPING_SECONDS
    return min(max(len(text) / TYPING_CHARS_PER_SECOND, low), high) + random.uniform(0, 1)


class SlidingWindowLimiter:
    """At most `limit` events per `window_seconds` for each key."""

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        events = self._events[key]
        while events and now - events[0] >= self.window:
            events.popleft()
        if len(events) >= self.limit:
            return False
        events.append(now)
        return True


class SendSpacer:
    """Keeps a minimum, slightly random gap between two outgoing messages, across all chats."""

    def __init__(
        self,
        min_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.min_interval = min_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_sent = -math.inf

    async def wait_turn(self) -> None:
        async with self._lock:
            wait = self._last_sent + self.min_interval * random.uniform(1, 1.5) - self._clock()
            if wait > 0:
                await self._sleep(wait)
            self._last_sent = self._clock()
