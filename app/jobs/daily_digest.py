"""Daily digest (R10): once a day, each group gets the last 24 hours' highlights, decisions, deadlines
and unanswered questions.

Off unless DAILY_DIGEST_TIME is set. A message Jeli sends on its own is kept to the minimum: one per
group per day, only when there is something new, never twice (each run is claimed in the database,
so a restart cannot repeat it), and within the channel's anti-ban limits.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone

from app.answer.catchup import Catchup
from app.kb.store import Store

log = logging.getLogger(__name__)

JOB = "daily_digest"
PERIOD = timedelta(hours=24)

Post = Callable[[str, str], Awaitable[bool]]


def next_run(now: datetime, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


async def post_daily_digests(
    store: Store, catchup: Catchup, post: Post, chat_ids: list[str], language: str, now: datetime
) -> int:
    """Post today's digest in each group that has news and hasn't had it yet. Returns how many were posted."""
    posted = 0
    for chat_id in chat_ids:
        if not await store.claim_daily_run(f"{JOB}:{chat_id}", now.date()):
            continue  # already posted today, e.g. before a restart
        digest = await catchup.summarize(now - PERIOD, language, chat_ids=[chat_id], quiet_if_empty=True)
        if digest is None:
            log.info("Nothing new in %s: no daily digest", chat_id)
            continue
        if await post(chat_id, digest):
            posted += 1
        else:
            log.warning("Daily digest for %s not sent: the channel is paused or at its hourly limit", chat_id)
    return posted


async def run_daily(
    store: Store,
    catchup: Catchup,
    post: Post,
    chat_ids: list[str],
    at: time,
    language: str,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep=asyncio.sleep,
) -> None:
    log.info("Daily digest at %s UTC in %d group(s)", at.strftime("%H:%M"), len(chat_ids))
    while True:
        now = clock()
        await sleep((next_run(now, at) - now).total_seconds())
        try:
            posted = await post_daily_digests(store, catchup, post, chat_ids, language, clock())
            log.info("Daily digest posted in %d group(s)", posted)
        except Exception:
            log.exception("Daily digest failed")
