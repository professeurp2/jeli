import asyncio
from datetime import datetime, timezone

from app.answer.language import TEXTS
from app.answer.responder import Responder
from app.models import IncomingMessage


def make_incoming(text, addressed_to_bot=True):
    return IncomingMessage(
        platform="whatsapp",
        chat_id="g@g.us",
        message_id="1",
        author="Awa",
        text=text,
        sent_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
        is_private=False,
        addressed_to_bot=addressed_to_bot,
    )


class FakeAnswerer:
    def __init__(self):
        self.questions = []

    async def answer(self, question, asker):
        self.questions.append((question, asker))
        return "answer"


def reply(text, answerer=None, addressed_to_bot=True):
    return asyncio.run(Responder(answerer).respond(make_incoming(text, addressed_to_bot)))


def test_stays_silent_when_not_addressed():
    assert reply("random chat", FakeAnswerer(), addressed_to_bot=False) is None


def test_questions_go_to_the_answerer():
    answerer = FakeAnswerer()
    assert reply("When is the bootcamp?", answerer) == "answer"
    assert answerer.questions == [("When is the bootcamp?", "Awa")]


def test_bare_mention_and_help_commands_explain_what_jeli_does():
    assert reply("", FakeAnswerer()) == TEXTS["en"]["help"]
    assert reply("/help", FakeAnswerer()) == TEXTS["en"]["help"]
    assert reply("aide", FakeAnswerer()) == TEXTS["fr"]["help"]


def test_commands_not_available_yet_get_the_help():
    answerer = FakeAnswerer()
    assert reply("/catchup since Monday", answerer) == TEXTS["en"]["help"]
    assert answerer.questions == []


def test_without_knowledge_base_jeli_says_it_is_not_ready_in_the_right_language():
    assert reply("Quand est le bootcamp ?") == TEXTS["fr"]["not_ready"]
    assert reply("When is the bootcamp?") == TEXTS["en"]["not_ready"]
