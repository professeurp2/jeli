"""Reminders a member asks for: "can you remind me before the meeting?" — and Jeli does.

The understanding step recognises the request; a model then works out what to remind and when,
from the conversation (Jeli has often just given the meeting's time) and the dated events Jeli
knows, and writes both the confirmation and the reminder itself in the member's words and
language. When the event or its time is unclear, it asks one short question instead.

The reminder is kept in the database (it survives restarts). An activity sends the reminders due,
every minute, in the chat where they were asked — as a reply to the member's request, with an
@mention in a group — within the channel's usual pace and limits. Jeli never messages first: a
reminder is the answer to a member's own request. One that could not be sent before the event
started is dropped rather than sent late.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel

from app.answer.citations import mention_tag
from app.answer.conversation import Turn, member_of
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import PERSONA
from app.answer.prompts import LANGUAGES
from app.answer.understand import conversation_text
from app.models import IncomingMessage, Reply

log = logging.getLogger(__name__)

MAX_ACTIVE_PER_MEMBER = 5
MAX_AHEAD = timedelta(days=60)
EVENTS_AHEAD_DAYS = 30
PLAN_TIMEOUT = 10
# A reminder sent this late is no longer a reminder: dropped (the server was down, WhatsApp off).
LATEST = timedelta(hours=2)
CAT = timedelta(hours=2)  # the cohort's common time zone, shown to the model beside UTC

PLAN_SYSTEM = PERSONA + """
Your task now: a member asks Jeli to remind them of something, or to cancel a reminder. From the
conversation so far, the dated events Jeli knows (below) and the member's message, return:
- action: "set" when you know what to remind and when; "clarify" when the event or its time is
  unknown or ambiguous (ask one short question in "reply", offering the options you see);
  "cancel" when they ask to cancel their reminder(s).
- what: the event, in a few words ("the needs assessment workshop").
- event_at: when the event starts, ISO 8601 in UTC ("2026-09-23T08:00:00Z"); convert from the time
  zone written (CAT = UTC+2, WAT = UTC+1, EAT = UTC+3, GMT = UTC); "" when only a day is known.
- remind_at: when to remind, ISO 8601 in UTC: the time the member asked ("an hour before", "the
  day before at 9", "tomorrow at 8"), otherwise 30 minutes before the event (when only a day is
  known: 07:00 UTC that day). Always after now.
- reply: what Jeli says now, in the member's language, one or two warm sentences: for "set", when
  you will remind them (day and time as the member reads them, e.g. "mercredi 23 septembre à 09:30
  CAT"); for "clarify", the question; for "cancel", that it is done.
- message: for "set", the reminder Jeli will send at remind_at, in the member's language: short and
  warm — the event, its time, how long until it starts. "" otherwise.
Use only the conversation and the events given: never invent an event, a date or a time.
"""


class ReminderPlan(BaseModel):
    action: str
    what: str = ""
    event_at: str = ""
    remind_at: str = ""
    reply: str = ""
    message: str = ""


def _moment(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


class Reminders:
    def __init__(self, store, llm: LLM | None, deadlines=None, clock=lambda: datetime.now(timezone.utc)):
        self.store = store
        self.llm = llm
        self.deadlines = deadlines  # the dated events Jeli knows (app.answer.deadlines.Deadlines)
        self.clock = clock

    async def _events(self, today: date) -> str:
        if self.store is None or not hasattr(self.store, "deadlines_between"):
            return ""
        found = await self.store.deadlines_between(today, today + timedelta(days=EVENTS_AHEAD_DAYS))
        return "\n".join(f"- {d.due_date:%A %d %B %Y}{', ' + d.due_time if d.due_time else ''}: {d.what}" for d in found[:40])

    async def handle(self, message: IncomingMessage, text: str, language: str, turns: list[Turn] = ()) -> Reply:
        """Set, clarify or cancel a reminder the member asked for; the reply to send now."""
        from app.answer.language import TEXTS

        texts = TEXTS[language]
        if self.llm is None or self.store is None:
            return Reply(texts["reminder_unavailable"])
        now = self.clock()
        prompt = (
            f"Now: {now:%A %d %B %Y, %H:%M} UTC ({now + CAT:%H:%M} CAT).\n"
            f"Dated events Jeli knows:\n{await self._events(now.date()) or '- none'}\n\n"
            + (f"Conversation so far:\n{conversation_text(list(turns))}\n\n" if turns else "")
            + f"Member's message: {text}\n\nWrite \"reply\" and \"message\" in {LANGUAGES.get(language, 'English')}."
        )
        try:
            plan = await self.llm.generate(prompt, ReminderPlan, system=PLAN_SYSTEM, timeout=PLAN_TIMEOUT, temperature=0.2, attempts=2)
        except LLMUnavailable:
            return Reply(texts["reminder_unavailable"])
        member = member_of(message)
        action = plan.action.strip().lower()
        if action == "cancel":
            cancelled = await self.store.cancel_reminders(member, message.chat_id)
            return Reply(plan.reply.strip() if cancelled and plan.reply.strip() else texts["reminder_none"] if not cancelled else texts["reminder_cancelled"])
        remind_at = _moment(plan.remind_at)
        if action != "set" or remind_at is None or not plan.message.strip():
            return Reply(plan.reply.strip() or texts["reminder_when"])
        if not now + timedelta(minutes=1) <= remind_at <= now + MAX_AHEAD:
            return Reply(texts["reminder_when"])
        if len(await self.store.active_reminders(member)) >= MAX_ACTIVE_PER_MEMBER:
            return Reply(texts["reminder_too_many"].format(limit=MAX_ACTIVE_PER_MEMBER))
        await self.store.add_reminder(
            platform=message.platform, chat_id=message.chat_id, message_id=message.message_id, member_key=member,
            member_id=message.author_id or "", what=plan.what.strip()[:200] or text[:200], event_at=_moment(plan.event_at),
            remind_at=remind_at, language=language, message=plan.message.strip()[:1000],
        )
        log.info("Reminder set for %s in %s at %s: %s", member, message.chat_id, remind_at.isoformat(), plan.what)
        return Reply(plan.reply.strip() or texts["reminder_set"])

    async def send_due(self, send) -> tuple[int, int]:
        """Send the reminders due; `send(chat_id, text, reply_to, mentions)` returns whether it went.
        Returns (sent, dropped). One not sent stays due until it is too late."""
        now = self.clock()
        sent = dropped = 0
        for reminder in await self.store.due_reminders(now):
            too_late = now - reminder["remind_at"] > LATEST or (reminder["event_at"] is not None and now >= reminder["event_at"])
            if reminder["platform"] != "whatsapp" or too_late:
                # A try from the dashboard has no chat to send to; a late one is no longer a reminder.
                await self.store.finish_reminder(reminder["id"], sent=False)
                dropped += 1
                continue
            text, mentions = reminder["message"], []
            if reminder["chat_id"].endswith("@g.us") and reminder["member_id"]:
                text, mentions = f"{mention_tag(reminder['member_id'])} {text}", [reminder["member_id"]]
            try:
                went = await send(reminder["chat_id"], text, reminder["message_id"] or None, mentions)
            except Exception:
                log.exception("Could not send reminder %s", reminder["id"])
                went = False
            if went:
                await self.store.finish_reminder(reminder["id"], sent=True)
                sent += 1
        return sent, dropped
