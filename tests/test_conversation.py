import asyncio
import dataclasses
from datetime import datetime, timezone

from app.answer.conversation import Conversations
from app.answer.intents import catchup_since
from app.answer.language import TEXTS
from app.answer.responder import Responder
from app.answer.understand import Understander, Understood
from app.models import IncomingMessage


def message(text, author_id="22370000000@c.us", addressed=False, **fields):
    return IncomingMessage(
        "whatsapp", "g@g.us", "1", "Awa", text, datetime(2026, 9, 19, tzinfo=timezone.utc), False, addressed,
        author_id=author_id, **fields,
    )


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


def test_after_an_answer_the_member_need_not_repeat_jeli():
    clock = Clock()
    conversations = Conversations(window_minutes=5, clock=clock)
    conversations.note(message("@Jeli when is the deadline?", addressed=True), "Thursday 24 September.")
    assert conversations.is_follow_up(message("And what do we submit?"))
    assert conversations.is_follow_up(message("et pour la vidéo ?"))
    assert conversations.is_follow_up(message("send me the guidelines please"))
    assert not conversations.is_follow_up(message("lol"))  # not a follow-up
    assert not conversations.is_follow_up(message("And you, Moussa?", talks_to_someone_else=True))
    assert not conversations.is_follow_up(message("And what do we submit?", author_id="22399999999@c.us"))  # someone else
    clock.now += 6 * 60
    assert not conversations.is_follow_up(message("And what do we submit?"))  # the conversation is over


def test_thanks_closes_the_conversation():
    conversations = Conversations(clock=Clock())
    conversations.note(message("@Jeli when?", addressed=True), "Thursday.")
    assert not conversations.is_follow_up(message("Thanks!"))
    assert not conversations.is_follow_up(message("And what do we submit?"))


def test_everyday_messages_get_a_human_reply_without_the_model():
    understander = Understander(None)

    def kind(text, language="en"):
        return asyncio.run(understander.understand(text, language)).kind

    assert kind("Hello Jeli") == "social" and kind("Merci !", "fr") == "social"
    assert kind("Who are you?") == "about_jeli" and kind("Qui es-tu ?", "fr") == "about_jeli"
    assert kind("What can you do?") == "about_jeli"
    assert kind("When is the deadline for the hackathon?") == "question"


def test_what_happened_today_is_a_catch_up():
    assert catchup_since("What happened today in the group?")
    assert catchup_since("Qu'est-ce qui s'est passé aujourd'hui ?")
    assert not catchup_since("What happened during the Module 1 session?")


class Answerer:
    def __init__(self):
        self.asked = []

    async def answer(self, question, asker, **context):
        self.asked.append((question, context.get("queries")))
        return "answer"

    async def prefetch(self, text):
        return []


class Understands:
    """Rewrites follow-ups with the conversation, as the model does."""

    def __init__(self):
        self.turns = []

    async def understand(self, text, language, turns=(), **kwargs):
        self.turns.append(list(turns))
        if text.startswith("And what"):
            return Understood(kind="question", reply="", standalone="What do we submit for the hackathon?", queries=["hackathon submission"])
        return Understood(kind="question", reply="", standalone=text, queries=[])


def test_a_follow_up_is_answered_with_the_conversation():
    answerer, understands = Answerer(), Understands()
    responder = Responder(answerer, understander=understands)
    asyncio.run(responder.respond(message("When is the hackathon deadline?", addressed=True)))
    follow_up = message("And what do we submit?")
    assert responder.is_follow_up(follow_up)
    asyncio.run(responder.respond(dataclasses.replace(follow_up, addressed_to_bot=True)))
    assert answerer.asked[-1] == ("What do we submit for the hackathon?", ["hackathon submission"])
    assert understands.turns[-1][0].message == "When is the hackathon deadline?"


def test_small_talk_never_reaches_the_answerer():
    answerer = Answerer()
    responder = Responder(answerer, understander=Understander(None))
    assert asyncio.run(responder.respond(message("Who are you?", addressed=True))) == TEXTS["en"]["about_jeli"]
    assert asyncio.run(responder.respond(message("hello", addressed=True))) == TEXTS["en"]["greeting_reply"]
    assert answerer.asked == []


def test_a_catch_up_says_when_jeli_does_not_follow_the_groups_yet():
    from app.answer.catchup import Catchup

    class Store:
        async def messages_since(self, since, chat_ids=None, limit=1500):
            return []

        async def recordings_since(self, since):
            return []

        async def latest_message_at(self):
            return datetime(2026, 9, 18, 20, 3, tzinfo=timezone.utc)

    digest = asyncio.run(Catchup(Store(), None).summarize(datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc), "fr"))
    assert digest.startswith("Je ne suis pas encore dans le groupe, donc ma mémoire s'arrête le ven. 18 sept. à 20:03 GMT")
