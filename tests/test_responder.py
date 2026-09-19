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
