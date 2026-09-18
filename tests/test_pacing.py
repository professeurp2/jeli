import asyncio

from app.adapters.pacing import TYPING_SECONDS, SendSpacer, SlidingWindowLimiter, typing_duration


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept = getattr(self, "slept", 0) + seconds
        self.now += seconds


def test_limiter_blocks_beyond_the_limit_then_frees_up():
    clock = FakeClock()
    limiter = SlidingWindowLimiter(limit=2, window_seconds=60, clock=clock)
    assert limiter.allow("awa") and limiter.allow("awa")
    assert not limiter.allow("awa")
    assert limiter.allow("moussa"), "each member has their own budget"
    clock.now += 60
    assert limiter.allow("awa")


def test_typing_duration_grows_with_length_within_bounds():
    low, high = TYPING_SECONDS
    assert low <= typing_duration("ok") <= low + 1
    assert high <= typing_duration("x" * 10_000) <= high + 1


def test_spacer_keeps_a_gap_between_messages():
    clock = FakeClock()
    spacer = SendSpacer(min_interval=3, clock=clock, sleep=clock.sleep)

    async def scenario():
        await spacer.wait_turn()  # first message: no wait
        assert getattr(clock, "slept", 0) == 0
        await spacer.wait_turn()  # immediately after: must wait at least the interval
        assert clock.slept >= 3
        clock.slept = 0
        clock.now += 10
        await spacer.wait_turn()  # long after: no wait
        assert clock.slept == 0

    asyncio.run(scenario())
