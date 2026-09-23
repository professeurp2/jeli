"""What the groups said while Jeli was not listening.

WAHA delivers each message once, as it arrives. If Jeli is restarting, redeploying or — as on
23 September — crashed, those calls fail and the conversation goes on without it: the memory has a
hole, and nobody knows until someone asks about that morning and Jeli says it never happened.

WhatsApp itself still has them. At startup Jeli therefore looks for the silences in each followed
group over the last two days, and asks WAHA for what was said during the longest one. It never
answers any of it: those messages are hours old, and a bot that wakes up and replies to a whole
morning at once is exactly what gets a number restricted.

Looking for the silence, rather than reading from the last message Jeli holds, is what makes this
work. Measured on 23 September at 09:09: the first version asked from the newest message it had,
and by the time it ran, newer messages had already arrived — so the nine-hour hole behind them was
invisible and stayed empty. A hole is not always at the end.
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# How far back to look for a hole. Beyond this it is not a restart but a long absence, and the team
# should import the group's export rather than have Jeli replay days of history at boot.
MAX_GAP = timedelta(days=2)
# A silence shorter than this is a quiet moment, not a hole. Groups do go quiet at night, and
# asking for those hours again costs one request and changes nothing: duplicates are dropped on the
# message id. Being wrong here is cheap; missing a morning is not.
MIN_HOLE = timedelta(minutes=20)
# A delivered message is never delivered twice, so a small overlap is free, and a missing minute
# is not.
OVERLAP = timedelta(minutes=2)
PER_GROUP = 500


def longest_silence(times: list[datetime], window_start: datetime, now: datetime) -> datetime | None:
    """When the longest silence began, between the messages Jeli holds and up to now; None when the
    group never went quiet for long enough to be a hole.

    What comes *before* the first message of the window is not counted: Jeli may simply not have
    been in the group then, and treating that as a hole would make every start replay two days.
    """
    moments = sorted(times)
    if not moments:
        return None
    moments.append(now)
    gaps = [(moments[i + 1] - moments[i], moments[i]) for i in range(len(moments) - 1)]
    longest, began = max(gaps, key=lambda gap: gap[0])
    return began if longest >= MIN_HOLE and began >= window_start else None


async def catch_up(adapter, store, groups: list[str], now: datetime | None = None) -> int:
    """Remember what each group said while Jeli was away. Returns how many messages it read back."""
    if adapter is None or store is None or not groups:
        return 0
    now = now or datetime.now(timezone.utc)
    window_start = now - MAX_GAP
    latest = await store.latest_per_chat()
    added = 0
    for chat_id in groups:
        if chat_id not in latest:
            continue  # a group Jeli has never heard: its history is an import, not a catch-up
        try:
            times = await store.message_times(chat_id, window_start)
            began = longest_silence(times, window_start, now)
            if began is None:
                continue
            read = await adapter.remember_history(chat_id, began - OVERLAP, PER_GROUP)
        except Exception:
            log.exception("Could not catch up on what %s said while Jeli was away", chat_id)
            continue
        if read:
            log.warning("Read back %d messages of %s said from %s", read, chat_id, f"{began:%d %b %H:%M} UTC")
        added += read
    return added
