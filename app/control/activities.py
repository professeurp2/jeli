"""Jeli's background activities, which the team can switch off, run now or stop from the dashboard.

Each activity is one piece of work (index new messages, find deadlines, post the daily summary,
send the weekly report) run on a schedule. Its loop sleeps until the next run, and wakes up early
when the team changes its schedule or switches it back on.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

Work = Callable[[], Awaitable[str]]  # returns what it did, in words for the team
# When to run next, from now and the run planned before (None at first, or after being switched on).
NextRun = Callable[[datetime, datetime | None], datetime]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Activity:
    def __init__(
        self,
        key: str,
        name: str,
        description: str,
        work: Work,
        next_run: NextRun,
        enabled: Callable[[], bool],
        blocked: Callable[[], str | None] = lambda: None,
        clock: Callable[[], datetime] = _now,
    ):
        self.key, self.name, self.description = key, name, description
        self.work = work
        self.next_run = next_run
        self._enabled = enabled
        # Why it cannot run now, in words ("WhatsApp is not connected"), or None.
        self.blocked = blocked
        self.clock = clock
        self.next_at: datetime | None = None
        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_result = ""
        self.last_ok: bool | None = None
        self._loop: asyncio.Task | None = None
        self._current: asyncio.Task | None = None
        self._manual: asyncio.Task | None = None  # a run the team started, kept referenced
        self._wake = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._enabled()

    @property
    def running(self) -> bool:
        return self._current is not None and not self._current.done()

    def start(self) -> None:
        self._loop = asyncio.create_task(self._run_forever(), name=f"activity:{self.key}")

    def wake(self) -> None:
        """Recompute the next run: the schedule changed, or the activity was switched on or off."""
        self._wake.set()

    async def _run_forever(self) -> None:
        while True:
            self._wake.clear()
            now = self.clock()
            self.next_at = self.next_run(now, self.next_at) if self.enabled else None
            timeout = (self.next_at - now).total_seconds() if self.next_at else None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                continue  # woken up: look at the schedule again
            except TimeoutError:
                pass
            if self.enabled and not self.blocked():
                await self.run_once()

    def _begin(self) -> None:
        self.last_started = self.clock()
        self._current = asyncio.get_running_loop().create_task(self.work(), name=f"work:{self.key}")

    async def run_once(self, by: str = "") -> None:
        """Run the work now, unless it is already running. A stop cancels only the work."""
        if self.running:
            return
        self._begin()
        await self._finish(by)

    async def _finish(self, by: str) -> None:
        await asyncio.wait({self._current})
        task, self._current = self._current, None
        self.last_finished = self.clock()
        if task.cancelled():
            self.last_ok, self.last_result = None, "Stopped before the end"
        elif task.exception():
            log.error("%s failed", self.name, exc_info=task.exception())
            self.last_ok, self.last_result = False, "Something went wrong; it will try again at the next run"
        else:
            self.last_ok, self.last_result = True, task.result()
            log.info("%s%s: %s", self.name, f" (started by {by})" if by else "", self.last_result)

    def run_now(self, by: str) -> bool:
        if self.running:
            return False
        self._begin()  # running from now on, so a second click cannot start it twice
        self._manual = asyncio.get_running_loop().create_task(self._finish(by))
        return True

    def stop(self) -> bool:
        if not self.running:
            return False
        self._current.cancel()
        return True

    async def close(self) -> None:
        for task in (self._current, self._manual, self._loop):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


def every(interval: timedelta, first: timedelta | None = None) -> NextRun:
    """Every interval; the first run after `first` (default: one interval). A planned run is kept
    when the loop wakes up for another reason, so a settings change never postpones it."""

    def next_run(now: datetime, planned: datetime | None) -> datetime:
        if planned is None:
            return now + (first if first is not None else interval)
        return planned if planned > now else now + interval

    return next_run
