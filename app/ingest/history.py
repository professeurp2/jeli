"""What the groups said while Jeli was not listening.

WAHA delivers each message once, as it arrives. If Jeli is restarting, redeploying or — as on
23 September — crashed, those calls fail and the conversation goes on without it: the memory has a
hole, and nobody knows until someone asks about that morning and Jeli says it never happened.

WhatsApp itself still has them. At startup Jeli therefore asks WAHA for each followed group's
messages since the last one it holds, and remembers them. It never answers them: they are hours
old, and a bot that wakes up and replies to a whole morning at once is exactly what gets a number
restricted.
"""

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# How far back to ask. Beyond this, a gap is not a restart but a long absence, and the team should
# import the group's export rather than have Jeli replay days of history at boot.
MAX_GAP = timedelta(days=2)
# A restart takes seconds; a message is delivered once. A small overlap is free — duplicates are
# dropped on the message id — and a missing minute is not.
OVERLAP = timedelta(minutes=2)
PER_GROUP = 300


async def catch_up(adapter, store, groups: list[str], now: datetime | None = None) -> int:
    """Remember what each group said while Jeli was away. Returns how many messages were new."""
    if adapter is None or store is None or not groups:
        return 0
    now = now or datetime.now(timezone.utc)
    latest = await store.latest_per_chat()
    added = 0
    for chat_id in groups:
        since = latest.get(chat_id)
        if since is None:
            continue  # a group Jeli has never heard: its history is an import, not a catch-up
        gap = now - since
        if gap < OVERLAP or gap > MAX_GAP:
            continue
        try:
            new = await adapter.remember_history(chat_id, since - OVERLAP, PER_GROUP)
        except Exception:
            log.exception("Could not catch up on what %s said while Jeli was away", chat_id)
            continue
        if new:
            log.warning(
                "Caught up %d messages said in %s during the %.1f h Jeli was away", new, chat_id, gap.total_seconds() / 3600
            )
        added += new
    return added
