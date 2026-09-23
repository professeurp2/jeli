"""What the groups said while Jeli was away is read back — and never answered."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.ingest.history import MAX_GAP, MIN_HOLE, OVERLAP, catch_up, longest_silence

GROUP = "120363000000000000@g.us"
NOW = datetime(2026, 9, 23, 9, 10, tzinfo=timezone.utc)
WINDOW = NOW - MAX_GAP


def at(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=timezone.utc)


class Store:
    def __init__(self, latest, times=()):
        self.latest = dict(latest)
        self.times = list(times)

    async def latest_per_chat(self):
        return dict(self.latest)

    async def message_times(self, chat_id, since):
        return [t for t in self.times if t >= since]


class Channel:
    def __init__(self, found=5, fails=False):
        self.found, self.fails = found, fails
        self.asked = []

    async def remember_history(self, chat_id, since, limit):
        if self.fails:
            raise RuntimeError("WAHA is unreachable")
        self.asked.append((chat_id, since, limit))
        return self.found


def test_a_hole_behind_newer_messages_is_still_found():
    """Measured 23 Sep at 09:09: Jeli was down from 01:46 to 08:57, then messages resumed. Reading
    from the newest message it held left the nine-hour hole behind them empty."""
    times = [at(22, 23, 0), at(22, 23, 8), at(23, 8, 57), at(23, 9, 3)]
    channel, store = Channel(found=40), Store({GROUP: at(23, 9, 3)}, times)
    assert asyncio.run(catch_up(channel, store, [GROUP], now=NOW)) == 40
    chat, since, _ = channel.asked[0]
    assert chat == GROUP
    assert since == at(22, 23, 8) - OVERLAP  # from where the group fell silent, not from the end


def test_a_group_that_never_went_quiet_is_left_alone():
    times = [NOW - timedelta(minutes=5 * n) for n in range(576)]
    channel = Channel()
    assert asyncio.run(catch_up(channel, Store({GROUP: times[0]}, times), [GROUP], now=NOW)) == 0
    assert channel.asked == []


def test_a_silence_shorter_than_a_quiet_moment_is_not_a_hole():
    dense = [NOW - timedelta(minutes=n) for n in range(0, 2881, 10)]
    assert longest_silence(dense, WINDOW, NOW) is None
    assert MIN_HOLE > timedelta(minutes=10)
    # One real silence in the middle of a busy day is found.
    with_hole = [t for t in dense if not (timedelta(hours=2) < NOW - t < timedelta(hours=4))]
    assert longest_silence(with_hole, WINDOW, NOW) == NOW - timedelta(hours=4)


def test_what_came_before_the_window_is_not_a_hole():
    """Jeli may simply not have been in the group then: counting it would replay two days at boot."""
    # Three messages in the last quarter of an hour, and nothing at all before them in two days.
    times = [NOW - timedelta(minutes=n) for n in (15, 10, 5)]
    assert longest_silence(times, WINDOW, NOW) is None


def test_a_group_jeli_has_never_heard_is_left_alone():
    channel = Channel()
    assert asyncio.run(catch_up(channel, Store({}, []), [GROUP], now=NOW)) == 0
    assert channel.asked == []


def test_a_failure_to_read_the_history_never_stops_the_start():
    times = [at(22, 23, 8), at(23, 9, 3)]
    channel = Channel(fails=True)
    assert asyncio.run(catch_up(channel, Store({GROUP: at(23, 9, 3)}, times), [GROUP], now=NOW)) == 0


def test_nothing_happens_without_a_channel_or_a_memory():
    assert asyncio.run(catch_up(None, Store({}, []), [GROUP])) == 0
    assert asyncio.run(catch_up(Channel(), None, [GROUP])) == 0
    assert asyncio.run(catch_up(Channel(), Store({}, []), [])) == 0
