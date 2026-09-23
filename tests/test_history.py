"""What the groups said while Jeli was away is read back — and never answered."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.ingest.history import MAX_GAP, OVERLAP, catch_up

GROUP = "120363000000000000@g.us"
NOW = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


class Store:
    def __init__(self, latest):
        self.latest = dict(latest)

    async def latest_per_chat(self):
        return dict(self.latest)


class Channel:
    def __init__(self, found=5, fails=False):
        self.found, self.fails = found, fails
        self.asked = []

    async def remember_history(self, chat_id, since, limit):
        if self.fails:
            raise RuntimeError("WAHA is unreachable")
        self.asked.append((chat_id, since, limit))
        return self.found


def test_the_hole_left_by_a_crash_is_filled():
    """23 Sep: Jeli was down seven hours and the group went on without it."""
    channel = Channel(found=42)
    store = Store({GROUP: NOW - timedelta(hours=7)})
    assert asyncio.run(catch_up(channel, store, [GROUP], now=NOW)) == 42
    chat, since, _ = channel.asked[0]
    assert chat == GROUP
    # Asked from slightly before the last message it holds: a delivered message is never delivered
    # twice, and a missing minute is worse than a duplicate.
    assert since == NOW - timedelta(hours=7) - OVERLAP


def test_a_restart_of_a_few_seconds_asks_for_nothing():
    channel = Channel()
    store = Store({GROUP: NOW - timedelta(seconds=20)})
    assert asyncio.run(catch_up(channel, store, [GROUP], now=NOW)) == 0
    assert channel.asked == []


def test_a_long_absence_is_not_replayed_at_boot():
    """Beyond two days it is not a restart: the team imports the export instead."""
    channel = Channel()
    store = Store({GROUP: NOW - MAX_GAP - timedelta(hours=1)})
    assert asyncio.run(catch_up(channel, store, [GROUP], now=NOW)) == 0
    assert channel.asked == []


def test_a_group_jeli_has_never_heard_is_left_alone():
    channel = Channel()
    assert asyncio.run(catch_up(channel, Store({}), [GROUP], now=NOW)) == 0
    assert channel.asked == []


def test_a_failure_to_read_the_history_never_stops_the_start():
    channel = Channel(fails=True)
    store = Store({GROUP: NOW - timedelta(hours=3)})
    assert asyncio.run(catch_up(channel, store, [GROUP], now=NOW)) == 0


def test_nothing_happens_without_a_channel_or_a_memory():
    assert asyncio.run(catch_up(None, Store({}), [GROUP])) == 0
    assert asyncio.run(catch_up(Channel(), None, [GROUP])) == 0
    assert asyncio.run(catch_up(Channel(), Store({}), [])) == 0
