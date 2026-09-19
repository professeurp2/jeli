"""Weekly report to the team, in private: what the groups asked Jeli, what it could not answer, how it
was used, and what is due — the dashboard's news, without having to open it.

Off unless TEAM_REPORT_TIME ("mon 07:00", UTC) and TEAM_NUMBERS are set. Private messages a number
starts are what WhatsApp watches most, so the report goes only to the team (who have saved Jeli's
number and written to it once), only to numbers that are on WhatsApp, once a week and never twice
(each send is claimed in the database), a minute or so apart, within the channel's limits, and not
at all in a quiet week. No model call: it is built from the dashboard's data.
"""

import asyncio
import hashlib
import logging
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone

from app.answer.deadlines import Deadlines
from app.dashboard import KINDS, OUTCOME_WORDS, _pct
from app.kb.store import Store

log = logging.getLogger(__name__)

JOB = "team_report"
PERIOD = timedelta(days=7)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
LISTED = 10  # questions listed per section: the rest is on the dashboard
GAP_SECONDS = (40.0, 100.0)  # between two members: a person does not send five messages at once

PostPrivate = Callable[[str, str], Awaitable[bool]]


def parse_schedule(value: str) -> tuple[int, time]:
    """"mon 07:00" -> (0, 07:00). ValueError when it is not a day and a time."""
    day, _, clock = value.strip().lower().partition(" ")
    if day[:3] not in WEEKDAYS:
        raise ValueError(f"not a day of the week: {day!r}")
    return WEEKDAYS.index(day[:3]), time.fromisoformat(clock.strip())


def next_run(now: datetime, weekday: int, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    return candidate if candidate > now else candidate + timedelta(days=7)


def _question_lines(questions: list[tuple[datetime, str, str]], with_outcome: bool) -> list[str]:
    return [
        f"• {question} ({at:%a %d %b}" + (f", {OUTCOME_WORDS.get(outcome, outcome)})" if with_outcome else ")")
        for at, outcome, question in questions[:LISTED]
    ]


def build_report(usage: dict, since: datetime, now: datetime, coming_up: str | None, dashboard_url: str = "") -> str | None:
    """The week in one message, or None when there is nothing to tell."""
    questions = usage["by_kind"].get("question", 0)
    others = [f"{label} {usage['by_kind'][kind]}" for kind, label in KINDS if usage["by_kind"].get(kind)]
    if not questions and not others and not coming_up:
        return None
    outcomes = Counter()
    for _, outcome, n in usage["questions_by_day"]:
        outcomes[outcome] += n
    lines = [f"📊 *Jeli weekly report* · {since:%a %d %b} – {now:%a %d %b}"]
    if questions:
        median = f" · median reply {usage['median_ms'] / 1000:.1f} s" if usage["median_ms"] is not None else ""
        lines.append(
            f"Questions: {questions} · answered with sources {_pct(outcomes['answered'], questions)}"
            f" · “I don't know” {_pct(outcomes['dont_know'], questions)}{median}"
        )
    else:
        lines.append("No questions this week.")
    if others:
        lines.append(" · ".join(others))
    asked = usage["group_questions"]
    unanswered = [q for q in asked if q[1] == "dont_know"]
    if unanswered:
        lines += ["", f"❓ *Jeli could not answer* ({len(unanswered)}): answer in the group, or add a source", *_question_lines(unanswered, False)]
    if asked:
        shown = f"{LISTED} latest of {len(asked)}" if len(asked) > LISTED else str(len(asked))
        lines += ["", f"💬 *Questions from the groups* ({shown})", *_question_lines(asked, True)]
    if coming_up:
        lines += ["", coming_up]
    if dashboard_url:
        lines += ["", f"Dashboard: {dashboard_url}"]
    return "\n".join(lines)


async def send_team_reports(
    store: Store,
    deadlines: Deadlines | None,
    post_private: PostPrivate,
    numbers: list[str],
    now: datetime,
    dashboard_url: str = "",
    sleep=asyncio.sleep,
    gap: Callable[[], float] = lambda: random.uniform(*GAP_SECONDS),
) -> int:
    """Send this week's report to each member who hasn't had it. Returns how many were sent."""
    since = now - PERIOD
    usage = await store.usage_since(since)
    coming_up = await deadlines.coming_up_section("en", days=7, today=now.date()) if deadlines else None
    report = build_report(usage, since, now, coming_up, dashboard_url)
    if report is None:
        log.info("Quiet week: no team report")
        return 0
    sent = 0
    for number in numbers:
        # Claimed by a hash: the database never holds the team's numbers.
        job = f"{JOB}:{hashlib.sha256(number.encode()).hexdigest()[:16]}"
        if not await store.claim_daily_run(job, now.date()):
            continue  # already sent, e.g. before a restart
        if sent:
            await sleep(gap())
        if await post_private(number, report):
            sent += 1
        else:
            await store.release_daily_run(job, now.date())
            log.warning("Team report to the number ending in %s not sent", number[-2:])
    return sent


async def run_weekly(
    store: Store,
    deadlines: Deadlines | None,
    post_private: PostPrivate,
    numbers: list[str],
    weekday: int,
    at: time,
    dashboard_url: str = "",
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep=asyncio.sleep,
) -> None:
    log.info("Team report every %s at %s UTC to %d member(s)", WEEKDAYS[weekday], at.strftime("%H:%M"), len(numbers))
    while True:
        now = clock()
        await sleep((next_run(now, weekday, at) - now).total_seconds())
        try:
            sent = await send_team_reports(store, deadlines, post_private, numbers, clock(), dashboard_url)
            log.info("Team report sent to %d member(s)", sent)
        except Exception:
            log.exception("Team report failed")
