"""Jeli's background activities, wired to the running components and the team's settings."""

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.control.activities import Activity, every
from app.control.runtime import Runtime
from app.control.schedule import next_daily, next_weekly, parse_clock, parse_schedule
from app.jobs.daily_digest import post_daily_digests
from app.jobs.event_reminders import post_event_reminders
from app.jobs.team_report import send_team_reports
from app.answer.citations import ignored_keys
from app.kb.indexer import index_pending

# The scan shares the Gemini quota with answers: a backlog (a fresh import) is drained a few
# batches an hour rather than in one burst that would leave members without answers.
MAX_DEADLINE_BATCHES_PER_RUN = 10
# A live conversation is learned once quiet for this long.
#
# It is the larger half of the wait before a new message can be answered about: a minute here, plus
# up to one round of the memory job, made ninety seconds — long enough that someone asking about
# what was just said got told Jeli did not know. Twenty seconds is still far longer than the pause
# between two messages of the same thought, and nothing is lost by cutting early anyway: the
# messages held back stay pending and are re-chunked with whatever follows them.
SETTLE = timedelta(seconds=20)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def build_activities(state, settings: Settings, runtime: Runtime) -> dict[str, Activity]:
    store, embedder = state.store, state.embedder
    extractor, catchup, deadlines, whatsapp = state.extractor, state.catchup, state.deadlines, state.whatsapp
    activities: dict[str, Activity] = {}

    def not_connected() -> str | None:
        if whatsapp is None:
            return "WhatsApp is not set up"
        return None if whatsapp.status == "WORKING" else "Waiting for WhatsApp to be connected"

    if store and embedder:

        async def learn() -> str:
            created = await index_pending(
                store, embedder, settle=SETTLE, ignored=ignored_keys(runtime["ignored_authors"]), labels=runtime["chat_labels"]
            )
            return f"learned {_plural(created, 'new conversation')}" if created else "nothing new to learn"

        activities["memory"] = Activity(
            "memory",
            "Keeping Jeli's memory up to date",
            "Every few minutes, Jeli reads the new messages so that it can answer about them.",
            learn,
            every(timedelta(seconds=settings.index_interval_seconds), first=timedelta(seconds=20)),
            lambda: runtime["enabled.memory"],
        )

    brief = getattr(state, "brief", None)
    if brief is not None and brief.llm is not None:

        async def write_brief() -> str:
            result = await brief.refresh()
            for name in ("answerer", "awareness", "understander"):
                component = getattr(state, name, None)
                if component is not None:
                    component.brief = brief.text
            return result

        activities["brief"] = Activity(
            "brief",
            "Refreshing what Jeli knows of the community",
            "Every few hours, Jeli rewrites its short brief of the programmes, organisers, rules and dates from "
            "the documents, the organisers' announcements and the session recaps — its general knowledge.",
            write_brief,
            every(timedelta(hours=6), first=timedelta(minutes=2 if not brief.text else 180)),
            lambda: runtime["enabled.brief"],
        )

    if extractor:

        async def find_deadlines() -> str:
            added = await extractor.run(max_batches=MAX_DEADLINE_BATCHES_PER_RUN)
            return f"found {_plural(added, 'new deadline')}" if added else "no new deadline"

        activities["deadlines"] = Activity(
            "deadlines",
            "Finding deadlines",
            "Every hour, Jeli looks for dates members must not miss in the new messages and sessions.",
            find_deadlines,
            every(timedelta(hours=1), first=timedelta(minutes=10)),
            lambda: runtime["enabled.deadlines"],
        )

    reminders = getattr(state, "reminders", None)
    if reminders is not None and whatsapp is not None:

        async def send_reminders() -> str:
            sent, dropped = await reminders.send_due(whatsapp.remind)
            if not sent and not dropped:
                return "no reminder due"
            return f"sent {_plural(sent, 'reminder')}" + (f", dropped {dropped} too late" if dropped else "")

        activities["reminders"] = Activity(
            "reminders",
            "Reminders members asked for",
            "Every minute, Jeli sends the reminders members asked for (\u201cremind me before the meeting\u201d), "
            "in the chat where they asked. One that cannot go before the event starts is dropped.",
            send_reminders,
            every(timedelta(minutes=1), first=timedelta(seconds=30)),
            lambda: runtime["enabled.reminders"],
            blocked=not_connected,
        )

    if store and whatsapp is not None:

        async def event_reminders() -> str:
            now = datetime.now(timezone.utc)
            groups = [g for g in runtime["groups"] if g not in set(runtime["silent_groups"])] or await store.live_groups(now - timedelta(days=7))
            posted = await post_event_reminders(store, whatsapp.post, groups, runtime["daily_digest_language"], now)
            return f"posted {_plural(posted, 'reminder')}" if posted else "no event starting in about an hour"

        activities["event_reminders"] = Activity(
            "event_reminders",
            "A word before each session",
            "About an hour before a scheduled session, Open Hour or deadline with a time (found in the "
            "announcements), Jeli says so in the groups — once per event. Off unless the team switches it on.",
            event_reminders,
            every(timedelta(minutes=15), first=timedelta(minutes=1)),
            lambda: runtime["enabled.event_reminders"],
            blocked=not_connected,
        )

    if catchup and whatsapp is not None:

        async def daily_summary() -> str:
            now = datetime.now(timezone.utc)
            groups = runtime["groups"] or await store.live_groups(now - timedelta(days=7))
            posted = await post_daily_digests(store, catchup, whatsapp.post, groups, runtime["daily_digest_language"], now)
            return f"posted in {_plural(posted, 'group')}" if posted else "nothing new to post"

        activities["daily_summary"] = Activity(
            "daily_summary",
            "Daily summary in the groups",
            "Once a day, each group gets the highlights, decisions, deadlines and open questions of the last "
            "24 hours — only when something happened, and never twice.",
            daily_summary,
            lambda now, planned: next_daily(now, parse_clock(runtime["daily_digest_time"])),
            lambda: runtime["enabled.daily_summary"],
            blocked=not_connected,
        )

    if store and whatsapp is not None:
        dashboard_url = f"https://{settings.railway_public_domain}/dashboard" if settings.railway_public_domain else ""

        async def team_report() -> str:
            sent = await send_team_reports(
                store, deadlines, whatsapp.post_private, settings.team_number_list, datetime.now(timezone.utc), dashboard_url
            )
            return f"sent to {_plural(sent, 'team member')}" if sent else "nothing sent (quiet week, or already sent)"

        def report_blocked() -> str | None:
            if not settings.team_number_list:
                return "No team phone numbers are set on the server"
            return not_connected()

        activities["team_report"] = Activity(
            "team_report",
            "Weekly report to the team",
            "Once a week, each team member gets the week's questions, knowledge gaps and deadlines in a "
            "private WhatsApp message.",
            team_report,
            lambda now, planned: next_weekly(now, *parse_schedule(runtime["team_report_time"])),
            lambda: runtime["enabled.team_report"],
            blocked=report_blocked,
        )
    return activities
