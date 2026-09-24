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
        found = [
            r
            for r in await self.active_reminders(member_key)
            if chat_id in (r["chat_id"], r.get("asked_in") or r["chat_id"])
        ]
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


PRIVATELY = SET.model_copy(update={
    "where": "private",
    "reply": "C'est noté ⏰ Je te préviens en privé mercredi à 09:30 CAT.",
})
GROUP = "120363429618850959@g.us"


def test_a_reminder_asked_for_in_private_arrives_in_private():
    """Asked in the group, delivered to the member alone — not to 240 people."""
    store = Store()
    reminders = Reminders(store, Plans(PRIVATELY), clock=lambda: NOW)
    reply = asyncio.run(reminders.handle(ask("rappelle-moi en privé avant la réunion"), "…", "fr", TURNS))
    assert reply == PRIVATELY.reply
    [kept] = store.rows
    assert kept["chat_id"] == "22370000000@c.us"  # the member's own chat, not the group
    assert kept["asked_in"] == GROUP  # but Jeli remembers where it was asked for
    # A message in one chat cannot be replied to from another, and nobody is mentioned in private.
    assert kept["message_id"] == ""
    sent = []

    async def send(chat_id, text, reply_to, mentions):
        sent.append((chat_id, text, reply_to, mentions))
        return True

    on_time = Reminders(store, None, clock=lambda: datetime(2026, 9, 23, 7, 30, 20, tzinfo=timezone.utc))
    assert asyncio.run(on_time.send_due(send)) == (1, 0)
    [(chat, text, reply_to, mentions)] = sent
    assert chat == "22370000000@c.us" and reply_to is None and mentions == []
    assert text == PRIVATELY.message and not text.startswith("@")


def test_a_private_reminder_is_cancelled_from_where_it_was_asked_for():
    store = Store()
    asyncio.run(Reminders(store, Plans(PRIVATELY), clock=lambda: NOW).handle(ask("rappelle-moi en privé"), "…", "fr", TURNS))
    cancel = ReminderPlan(action="cancel", reply="C'est fait 👍")
    asyncio.run(Reminders(store, Plans(cancel), clock=lambda: NOW).handle(ask("annule mon rappel"), "annule", "fr"))
    assert store.rows[0]["cancelled"] is True


def test_a_group_member_is_reached_by_the_number_behind_their_group_id():
    """In a group everyone arrives as a LID, which is not a chat anyone can open."""
    from app.answer.citations import NUMBER_OF_LID, private_chat_of

    assert private_chat_of("216324735279308@lid") == ""  # not known yet: no guessing
    NUMBER_OF_LID["216324735279308"] = "22370000000"
    try:
        assert private_chat_of("216324735279308@lid") == "22370000000@c.us"
        store = Store()
        message = ask("rappelle-moi en privé")
        message = message.__class__(**{**message.__dict__, "author_id": "216324735279308@lid"})
        asyncio.run(Reminders(store, Plans(PRIVATELY), clock=lambda: NOW).handle(message, "…", "fr", TURNS))
        assert store.rows[0]["chat_id"] == "22370000000@c.us"
    finally:
        NUMBER_OF_LID.pop("216324735279308", None)


def test_when_the_private_chat_is_unknown_jeli_says_so_instead_of_telling_the_group():
    """The reminder a member wanted kept quiet must never fall back to the group."""
    store = Store()
    message = ask("rappelle-moi en privé")
    message = message.__class__(**{**message.__dict__, "author_id": "999888777666@lid"})
    reply = asyncio.run(Reminders(store, Plans(PRIVATELY), clock=lambda: NOW).handle(message, "…", "fr", TURNS))
    assert "en privé" in reply and store.rows == []


def test_asking_in_private_changes_nothing_and_the_normal_case_is_unchanged():
    store = Store()
    asyncio.run(Reminders(store, Plans(PRIVATELY), clock=lambda: NOW).handle(
        ask("rappelle-moi", chat_id="22370000000@c.us"), "…", "fr", TURNS))
    assert store.rows[0]["chat_id"] == "22370000000@c.us" and store.rows[0]["message_id"] == "req-1"
    store = Store()
    asyncio.run(Reminders(store, Plans(SET), clock=lambda: NOW).handle(ask("rappelle-moi"), "…", "fr", TURNS))
    assert store.rows[0]["chat_id"] == GROUP and store.rows[0]["asked_in"] == GROUP


def test_jeli_knows_it_can_remind_privately():
    from app.answer.persona import CAPABILITIES
    from app.answer.reminders import PLAN_SYSTEM

    assert "or privately, just to them, when they ask for that" in CAPABILITIES
    assert '"private" when the member asks' in PLAN_SYSTEM and "any words and any language" in PLAN_SYSTEM
