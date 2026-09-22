"""A word in the groups about an hour before a scheduled session, Open Hour or deadline with a time.

Seen in the cohort's group on 21 Sep: the bot members rated best posted "⏰ Open Hour with Gift
starts in 1 hour — at 15:00 CAT / 14:00 WAT / 16:00 EAT", and members thanked it. Jeli knows the
same events (its deadlines, found in the announcements with their time); it says so once per event,
in the language the team chose, only when the team switched it on, within the channel's limits.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, timezone

from app.kb.store import Store
from app.models import Deadline

log = logging.getLogger(__name__)

JOB = "event_reminder"
# The window in which "in about an hour" is true: run every 15 minutes, catch events 45–75 min ahead.
AHEAD_MIN = timedelta(minutes=45)
AHEAD_MAX = timedelta(minutes=75)
# The time zones members write ("2:00 PM CAT", "15h WAT", "14:00 GMT"), as UTC offsets in hours.
ZONES = {"cat": 2, "sast": 2, "eat": 3, "wat": 1, "gmt": 0, "utc": 0, "cet": 1, "cest": 2, "west": 1, "bst": 1}
TIME = re.compile(
    r"(?P<h>\d{1,2})(?:[:h.](?P<m>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?\s*(?P<zone>cat|sast|eat|wat|gmt|utc|cet|cest|west|bst)?",
    re.IGNORECASE,
)
TEXTS = {
    "en": "⏰ *{what}* starts in about an hour — at {times}.",
    "fr": "⏰ *{what}* commence dans environ une heure — à {times}.",
}

Post = Callable[[str, str], Awaitable[bool]]


def event_moment(due: date, due_time: str, default_offset_hours: int = 2) -> datetime | None:
    """The UTC moment of a deadline's stated time ("2:00 PM CAT", "15:00"); None without a time."""
    match = TIME.search(due_time or "")
    if not match:
        return None
    hour, minute = int(match["h"]), int(match["m"] or 0)
    if match["ampm"]:
        hour = hour % 12 + (12 if match["ampm"].lower().startswith("p") else 0)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        return None
    offset = ZONES.get((match["zone"] or "").lower(), default_offset_hours)
    local = datetime.combine(due, datetime.min.time()).replace(hour=hour, minute=minute, tzinfo=timezone(timedelta(hours=offset)))
    return local.astimezone(timezone.utc)


def times_text(moment: datetime) -> str:
    """"15:00 CAT / 14:00 WAT / 16:00 EAT": the cohort's time zones, as the organisers write them."""
    return " / ".join(
        f"{(moment + timedelta(hours=offset)):%H:%M} {zone}" for zone, offset in (("CAT", 2), ("WAT", 1), ("EAT", 3))
    )


async def post_event_reminders(store: Store, post: Post, chat_ids: list[str], language: str, now: datetime) -> int:
    """Say in each group which events start in about an hour; once per event. Returns how many messages."""
    today = now.date()
    deadlines: list[Deadline] = await store.deadlines_between(today, today + timedelta(days=1))
    posted = 0
    for deadline in deadlines:
        moment = event_moment(deadline.due_date, deadline.due_time)
        if moment is None or not AHEAD_MIN <= moment - now <= AHEAD_MAX:
            continue
        if not await store.claim_daily_run(f"{JOB}:{deadline.id}", today):
            continue  # already said, e.g. before a restart
        text = TEXTS.get(language, TEXTS["en"]).format(what=deadline.what, times=times_text(moment))
        for chat_id in chat_ids:
            if await post(chat_id, text):
                posted += 1
            else:
                log.warning("Event reminder for %s not sent in %s: channel paused or at its limit", deadline.what, chat_id)
    return posted
