import asyncio
from datetime import date, datetime, timedelta, timezone

from app.answer.conversation import Turn
from app.answer.reminders import ReminderPlan, Reminders
from app.models import Deadline, IncomingMessage

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
WORKSHOP = Deadline("Needs assessment workshop with the Ethiopian AI Institute", date(2026, 9, 23), "meti", NOW, due_time="10:00 AM CAT")


class Store:
    def __init__(self):
        self.rows, self.finished = [], []

    async def deadlines_between(self, start, end, include_dismissed=False):
        return [WORKSHOP]

    async def add_reminder(self, **fields):
        self.rows.append({"id": len(self.rows) + 1, "sent_at": None, "cancelled": False, **fields})
        return len(self.rows)

    async def active_reminders(self, member_key):
        return [r for r in self.rows if r["member_key"] == member_key and r["sent_at"] is None and not r["cancelled"]]

    async def cancel_reminders(self, member_key, chat_id):
        found = [r for r in await self.active_reminders(member_key) if r["chat_id"] == chat_id]
        for r in found:
            r["cancelled"] = True
        return len(found)

    async def due_reminders(self, now):
        return [r for r in self.rows if r["remind_at"] <= now and r["sent_at"] is None and not r["cancelled"]]

    async def finish_reminder(self, reminder_id, sent):
        row = next(r for r in self.rows if r["id"] == reminder_id)
        row["sent_at"], row["sent"] = NOW, sent
        self.finished.append((reminder_id, sent))


class Plans:
    def __init__(self, plan):
        self.plan, self.prompts = plan, []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return self.plan


def ask(text, chat_id="120363429618850959@g.us", message_id="req-1"):
    return IncomingMessage("whatsapp", chat_id, message_id, "Awa", text, NOW, False, True, author_id="22370000000@c.us")


SET = ReminderPlan(
    action="set", what="the needs assessment workshop", event_at="2026-09-23T08:00:00Z", remind_at="2026-09-23T07:30:00Z",
    reply="C'est noté ⏰ Je te préviens mercredi 23 septembre à 09:30 CAT, 30 minutes avant l'atelier.",
    message="⏰ L'atelier d'évaluation des besoins commence à 10:00 CAT, dans 30 minutes !",
)
TURNS = [Turn(at=0, message="C'est quand la réunion ?", reply="L'atelier d'évaluation des besoins est mercredi 23 septembre de 10:00 à 11:30 CAT.")]


def test_asked_to_be_reminded_before_the_meeting_jeli_says_when_and_keeps_it():
    store, llm = Store(), Plans(SET)
    reminders = Reminders(store, llm, clock=lambda: NOW)
    reply = asyncio.run(reminders.handle(ask("Est-ce que tu peux me rappeler avant la réunion ?"), "Rappelle-moi avant la réunion", "fr", TURNS))
    assert reply == SET.reply
    [kept] = store.rows
    assert kept["remind_at"] == datetime(2026, 9, 23, 7, 30, tzinfo=timezone.utc) and kept["message_id"] == "req-1"
    assert kept["member_key"] == "22370000000" and kept["chat_id"] == "120363429618850959@g.us"
    prompt = llm.prompts[0]
    # The model works from the conversation (the meeting's time Jeli just gave) and the dated events.
    assert "Jeli: L'atelier d'évaluation des besoins est mercredi 23 septembre de 10:00 à 11:30 CAT." in prompt
    assert "Wednesday 23 September 2026, 10:00 AM CAT: Needs assessment workshop" in prompt
    assert prompt.startswith("Now: Tuesday 22 September 2026, 12:00 UTC (14:00 CAT).")


def test_an_unclear_or_past_reminder_is_asked_about_and_a_member_can_cancel():
    store = Store()
    unclear = ReminderPlan(action="clarify", reply="Tu parles de l'atelier de mercredi ou de la session Wadhwani de jeudi ?")
    assert asyncio.run(Reminders(store, Plans(unclear), clock=lambda: NOW).handle(ask("rappelle-moi"), "rappelle-moi", "fr")) == unclear.reply
    past = SET.model_copy(update={"remind_at": "2026-09-21T07:30:00Z"})
    assert asyncio.run(Reminders(store, Plans(past), clock=lambda: NOW).handle(ask("rappelle-moi"), "rappelle-moi", "fr")).startswith("Avec plaisir ⏰")
    assert store.rows == []
    asyncio.run(Reminders(store, Plans(SET), clock=lambda: NOW).handle(ask("rappelle-moi avant la réunion"), "…", "fr", TURNS))
    cancel = ReminderPlan(action="cancel", reply="C'est fait, rappel annulé 👍")
    assert asyncio.run(Reminders(store, Plans(cancel), clock=lambda: NOW).handle(ask("annule mon rappel"), "annule", "fr")) == cancel.reply
    assert store.rows[0]["cancelled"] is True


def test_the_reminder_goes_at_its_time_in_the_same_chat_replying_to_the_request():
    store = Store()
    asyncio.run(Reminders(store, Plans(SET), clock=lambda: NOW).handle(ask("rappelle-moi avant la réunion"), "…", "fr", TURNS))
    sent = []

    async def send(chat_id, text, reply_to, mentions):
        sent.append((chat_id, text, reply_to, mentions))
        return True

    early = Reminders(store, None, clock=lambda: datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc))
    assert asyncio.run(early.send_due(send)) == (0, 0) and sent == []
    on_time = Reminders(store, None, clock=lambda: datetime(2026, 9, 23, 7, 30, 20, tzinfo=timezone.utc))
    assert asyncio.run(on_time.send_due(send)) == (1, 0)
    [(chat, text, reply_to, mentions)] = sent
    assert chat == "120363429618850959@g.us" and reply_to == "req-1" and mentions == ["22370000000@c.us"]
    assert text == "@22370000000 ⏰ L'atelier d'évaluation des besoins commence à 10:00 CAT, dans 30 minutes !"
    assert asyncio.run(on_time.send_due(send)) == (0, 0)  # never twice


def test_a_reminder_that_could_not_go_before_the_event_is_dropped_not_sent_late():
    store = Store()
    asyncio.run(Reminders(store, Plans(SET), clock=lambda: NOW).handle(ask("rappelle-moi", chat_id="22370000000@c.us"), "…", "fr", TURNS))

    async def down(chat_id, text, reply_to, mentions):
        return False  # WhatsApp paused or disconnected

    assert asyncio.run(Reminders(store, None, clock=lambda: datetime(2026, 9, 23, 7, 31, tzinfo=timezone.utc)).send_due(down)) == (0, 0)
    assert store.rows[0]["sent_at"] is None  # still due: tried again next minute
    after = Reminders(store, None, clock=lambda: datetime(2026, 9, 23, 8, 5, tzinfo=timezone.utc))
    assert asyncio.run(after.send_due(down)) == (0, 1) and store.finished == [(1, False)]


def test_the_understanding_step_knows_a_reminder_request():
    from app.answer.understand import KINDS, SYSTEM

    assert "reminder" in KINDS and '"reminder": asks Jeli to remind them' in SYSTEM
