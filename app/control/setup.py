"""Jeli's background activities, wired to the running components and the team's settings."""

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.control.activities import Activity, every
from app.control.runtime import Runtime
from app.control.schedule import next_daily, next_weekly, parse_clock, parse_schedule
from app.ingest.chunker import MAX_GAP
from app.jobs.daily_digest import post_daily_digests
from app.jobs.team_report import send_team_reports
from app.kb.indexer import index_pending

# The scan shares the Gemini quota with answers: a backlog (a fresh import) is drained a few
# batches an hour rather than in one burst that would leave members without answers.
MAX_DEADLINE_BATCHES_PER_RUN = 10


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
            created = await index_pending(store, embedder, settle=MAX_GAP)
            return f"learned {_plural(created, 'new conversation')}" if created else "nothing new to learn"

        activities["memory"] = Activity(
            "memory",
            "Keeping Jeli's memory up to date",
            "Every few minutes, Jeli reads the new messages so that it can answer about them.",
            learn,
            every(timedelta(seconds=settings.index_interval_seconds), first=timedelta(seconds=20)),
            lambda: runtime["enabled.memory"],
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
