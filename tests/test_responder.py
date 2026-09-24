import asyncio
from datetime import datetime, timezone

from app.answer.language import TEXTS
from app.answer.responder import Responder
from app.models import IncomingMessage


def make_incoming(text, addressed_to_bot=True, is_private=False, author="Awa", author_id="22370000000@c.us"):
    return IncomingMessage(
        platform="whatsapp",
        chat_id="g@g.us",
        message_id="1",
        author=author,
        text=text,
        sent_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        is_private=is_private,
        addressed_to_bot=addressed_to_bot,
        author_id=author_id,
    )


class FakeAnswerer:
    def __init__(self, already=None, ignored=()):
        self.questions, self.checked = [], []
        self.already = already
        self.ignored = set(ignored)

    async def answer(self, question, asker, **context):
        self.questions.append((question, asker))
        return "answer"

    async def prefetch(self, text):
        return []

    async def where_discussed(self, topic):
        return f"sources for {topic}"

    async def already_answered(self, question, min_similarity, **context):
        self.checked.append(question)
        return self.already

    def is_ignored(self, message):
        return message.author in self.ignored


class FakeCatchup:
    def __init__(self):
        self.calls = []

    async def summarize(self, since, language):
        self.calls.append((since, language))
        return "digest"


def reply(text, answerer=None, catchup=None, responder=None, **incoming):
    responder = responder or Responder(answerer, catchup, duplicate_detection=True)
    return asyncio.run(responder.respond(make_incoming(text, **incoming)))


def test_questions_go_to_the_answerer():
    answerer = FakeAnswerer()
    assert reply("When is the bootcamp?", answerer) == "answer"
    assert answerer.questions == [("When is the bootcamp?", "Awa")]


def test_bare_mention_and_help_commands_explain_what_jeli_does():
    assert reply("", FakeAnswerer()) == TEXTS["en"]["help"]
    assert reply("/help", FakeAnswerer()) == TEXTS["en"]["help"]
    assert reply("aide", FakeAnswerer()) == TEXTS["fr"]["help"]


def test_catchup_requests_get_the_digest_in_the_right_language():
    catchup = FakeCatchup()
    assert reply("/catchup since Monday", FakeAnswerer(), catchup) == "digest"
    assert reply("Qu'est-ce que j'ai raté depuis lundi ?", FakeAnswerer(), catchup) == "digest"
    assert [language for _, language in catchup.calls] == ["en", "fr"]


def test_unknown_commands_get_the_help():
    answerer = FakeAnswerer()
    assert reply("/pizza", answerer) == TEXTS["en"]["help"]
    assert reply("/search", answerer) == TEXTS["en"]["help"]
    assert answerer.questions == []


def test_search_shows_where_the_group_talked_about_it():
    answerer = FakeAnswerer()
    assert reply("/search bootcamp venue", answerer) == "sources for bootcamp venue"


class FakeRecaps:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def reply(self, text, language):
        self.calls.append((text, language))
        return self.result

    async def answer(self, question, language):
        return None  # not about what was said in one named session


def test_session_recaps_and_fallback_to_a_normal_question():
    found = Responder(FakeAnswerer(), recaps=FakeRecaps("🎥 recap"))
    assert reply("Summary of the Module 1 session?", responder=found) == "🎥 recap"
    assert reply("/recap 2", responder=found) == "🎥 recap"
    # Not about a recorded session: answered as a question.
    unknown = Responder(FakeAnswerer(), recaps=FakeRecaps(None))
    assert reply("Summary of the bootcamp session?", responder=unknown) == "answer"


def test_without_knowledge_base_jeli_says_it_is_not_ready_in_the_right_language():
    assert reply("Quand est le bootcamp ?") == TEXTS["fr"]["not_ready"]
    assert reply("When is the bootcamp?") == TEXTS["en"]["not_ready"]
    assert reply("/catchup") == TEXTS["en"]["not_ready"]


def test_group_chatter_that_is_not_a_question_is_never_checked():
    answerer = FakeAnswerer(already="💡 already")
    assert reply("Thanks everyone, see you tomorrow", answerer, addressed_to_bot=False) is None
    assert answerer.checked == []


def test_a_question_the_group_already_answered_gets_a_pointer():
    answerer = FakeAnswerer(already="💡 already")
    assert reply("When is the deadline for the hackathon?", answerer, addressed_to_bot=False) == "💡 already"


def test_new_questions_get_no_uninvited_reply():
    assert reply("When is the deadline for the hackathon?", FakeAnswerer(already=None), addressed_to_bot=False) is None


def test_uninvited_replies_are_capped_per_group():
    answerer = FakeAnswerer(already="💡 already")
    responder = Responder(answerer, duplicate_detection=True, duplicate_replies_per_hour=2)
    replies = [reply(f"Where is the link {n}?", responder=responder, addressed_to_bot=False) for n in range(4)]
    assert replies == ["💡 already", "💡 already", None, None]


def test_duplicate_detection_can_be_switched_off_and_ignores_other_bots():
    answerer = FakeAnswerer(already="💡 already", ignored={"OtherBot"})
    off = Responder(answerer, duplicate_detection=False)
    assert reply("When is the deadline?", responder=off, addressed_to_bot=False) is None
    assert reply("When is the deadline?", answerer, addressed_to_bot=False, author="OtherBot") is None
    assert answerer.checked == []


# --- A member telling Jeli who they are --------------------------------------------------------


class Directory:
    """The little Jeli keeps about each member, and the reminders waiting on it."""

    def __init__(self, waiting=0):
        self.kept, self.settled, self.waiting = {}, [], waiting

    async def remember_member(self, member_key, name="", language="", notes=None, number=""):
        held = self.kept.setdefault(member_key, {"name": "", "number": ""})
        held["name"] = name or held["name"]
        held["number"] = number or held["number"]

    async def settle_reminders(self, member_key, chat_id):
        self.settled.append((member_key, chat_id))
        return self.waiting


class Says:
    """An understanding step that read the message as an introduction."""

    def __init__(self, **fields):
        from app.answer.understand import Understood

        self.understood = Understood(kind="identity", language="fr", **fields)

    async def understand(self, text, language, turns, member=""):
        return self.understood


def _reply(understander, store, text="je m'appelle Awa, mon numéro c'est +223 93 05 69 36"):
    from app.answer.conversation import Conversations

    responder = Responder(FakeAnswerer(), understander=understander, store=store,
                          conversations=Conversations())
    return asyncio.run(responder.respond(make_incoming(text)))


def test_a_member_who_says_who_they_are_is_remembered():
    """Jeli knows members by the id WhatsApp hands it — not a name, not a number. Now it can be told."""
    store = Directory()
    reply = _reply(Says(person_name="Awa", person_number="+223 93 05 69 36"), store)
    assert store.kept == {"22370000000": {"name": "Awa", "number": "22393056936"}}
    assert "Awa" in reply and "numéro" in reply
    # And whatever was waiting on it was asked to go ahead, addressed to that number.
    assert store.settled == [("22370000000", "22393056936@c.us")]


def test_the_reminder_that_was_waiting_goes_ahead_by_itself():
    """Nobody should have to ask twice: the reminder they set up before Jeli knew them is settled."""
    store = Directory(waiting=1)
    reply = _reply(Says(person_name="Awa", person_number="0022393056936"), store)
    assert TEXTS["fr"]["identity_settled"] in reply
    assert store.settled == [("22370000000", "22393056936@c.us")]


def test_a_name_alone_is_kept_without_pretending_to_have_a_number():
    store = Directory()
    reply = _reply(Says(person_name="Awa"), store, "moi c'est Awa")
    assert store.kept == {"22370000000": {"name": "Awa", "number": ""}}
    assert store.settled == []  # nothing to address: no reminder is quietly sent anywhere
    assert reply == TEXTS["fr"]["identity_kept"].format(name="Awa")


def test_something_that_is_not_a_number_is_not_taken_for_one():
    """A year, a room, an amount: Jeli asks again rather than writing to a stranger."""
    store = Directory()
    reply = _reply(Says(person_name="Awa", person_number="2026"), store, "je suis Awa, salle 2026")
    assert reply == TEXTS["fr"]["identity_unclear"]
    # No number is kept, and nothing waiting is addressed to a room number.
    assert not store.kept.get("22370000000", {}).get("number") and store.settled == []


def test_the_number_itself_is_never_written_to_the_log(caplog):
    import logging

    store = Directory()
    with caplog.at_level(logging.INFO):
        _reply(Says(person_name="Awa", person_number="+223 93 05 69 36"), store)
    printed = " ".join(record.getMessage() for record in caplog.records)
    assert "22393056936" not in printed and "93 05 69 36" not in printed
    assert "told Jeli who they are" in printed  # that it happened is worth knowing; the number is not
