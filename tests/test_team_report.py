import asyncio
from datetime import date, datetime, time, timezone

import pytest

from app.config import Settings
from app.jobs.team_report import build_report, next_run, parse_schedule, send_team_reports

SAT = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
MON_7 = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
WEEK_BEFORE = datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc)

USAGE = {
    "by_kind": {"question": 12, "catchup": 3, "search": 1},
    "questions_by_day": [(date(2026, 9, 18), "answered", 9), (date(2026, 9, 19), "dont_know", 3)],
    "median_ms": 2800.0,
    "p95_ms": 6400.0,
    "group_questions": [
        (datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc), "dont_know", "Who judges the bots?"),
        (datetime(2026, 9, 18, 9, 0, tzinfo=timezone.utc), "answered", "When is the submission deadline?"),
    ],
}
QUIET = {"by_kind": {}, "questions_by_day": [], "median_ms": None, "p95_ms": None, "group_questions": []}


def test_schedule_is_a_weekday_and_a_time():
    assert parse_schedule("mon 07:00") == (0, time(7, 0))
    assert parse_schedule(" Friday 18:30 ") == (4, time(18, 30))
    for wrong in ("07:00", "someday 07:00", "mon"):
        with pytest.raises(ValueError):
            parse_schedule(wrong)
    assert next_run(SAT, 0, time(7, 0)) == MON_7
    assert next_run(MON_7, 0, time(7, 0)) == datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)  # just sent: next week


def test_team_numbers_are_digits_only():
    settings = Settings(_env_file=None, team_numbers="+234 706 931 0683, +223 93 05 69 36,,+234 706 931 0683")
    assert settings.team_number_list == ["2347069310683", "22393056936"]


def test_report_tells_the_week_in_one_message():
    report = build_report(USAGE, WEEK_BEFORE, MON_7, "⏰ Coming up\n• Thu 24 Sep — Hackathon: submit", "https://jeli.test/dashboard")
    lines = report.splitlines()
    assert lines[0] == "📊 *Jeli weekly report* · Mon 14 Sep – Mon 21 Sep"
    assert lines[1] == "Questions: 12 · answered with sources 75% · “I don't know” 25% · median reply 2.8 s"
    assert lines[2] == "Catch-ups 3 · Searches 1"
    assert "• Who judges the bots? (Sat 19 Sep)" in lines
    assert "• When is the submission deadline? (Fri 18 Sep, answered)" in lines
    assert "• Thu 24 Sep — Hackathon: submit" in lines and lines[-1] == "Dashboard: https://jeli.test/dashboard"


def test_a_quiet_week_sends_nothing():
    assert build_report(QUIET, WEEK_BEFORE, MON_7, None) is None
    assert build_report(QUIET, WEEK_BEFORE, MON_7, "⏰ Coming up\n• Thu 24 Sep — Submit").startswith("📊")


class FakeStore:
    def __init__(self, usage):
        self.usage, self.claims = usage, set()

    async def usage_since(self, since):
        return self.usage

    async def claim_daily_run(self, job, day):
        if (job, day) in self.claims:
            return False
        self.claims.add((job, day))
        return True

    async def release_daily_run(self, job, day):
        self.claims.discard((job, day))


def test_each_member_gets_the_report_once_spaced_out():
    store, sent, pauses = FakeStore(USAGE), [], []

    numbers = ["2347069310683", "256781202892", "261342348263"]

    async def post_private(number, text):
        sent.append(number)
        return number != numbers[2]  # not on WhatsApp

    async def sleep(seconds):
        pauses.append(seconds)

    def run():
        return asyncio.run(send_team_reports(store, None, post_private, numbers, MON_7, sleep=sleep, gap=lambda: 60))

    assert run() == 2
    assert sent == numbers and pauses == [60, 60]
    assert len(store.claims) == 2  # the failed send was released, for a later run
    assert not any(number in job for job, _ in store.claims for number in numbers)  # numbers never stored
    sent.clear()
    assert run() == 0 and sent == [numbers[2]]  # after a restart: only the one who didn't get it


def test_no_report_in_a_quiet_week():
    sent = []

    async def post_private(number, text):
        sent.append(number)
        return True

    assert asyncio.run(send_team_reports(FakeStore(QUIET), None, post_private, ["1"], MON_7)) == 0
    assert sent == []
