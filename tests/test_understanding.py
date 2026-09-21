"""The understanding step, the persisted conversation, the bounded model fallback and the
verified sources — what turned "not intelligent" into a colleague (21 Sep)."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from google.genai import errors

from app.answer.conversation import Conversations, Turn
from app.answer.language import TEXTS
from app.answer.llm import LLM, MAX_ATTEMPTS, GeneratedAnswer
from app.answer.persona import background
from app.answer.rag import parse_source, supports
from app.answer.responder import Responder
from app.answer.understand import Understander, Understood, plain
from app.models import IncomingMessage, Reply


def message(text, addressed=True, author_id="22370000000@c.us", chat_id="g@g.us"):
    return IncomingMessage("whatsapp", chat_id, "1", "Awa", text, datetime(2026, 9, 21, tzinfo=timezone.utc), False, addressed, author_id=author_id)


def understood(text, language="en"):
    return asyncio.run(Understander(None).understand(text, language))


def test_everyday_messages_never_need_the_model():
    assert understood("jeli").kind == "social"
    assert understood("Thank you Jeli").kind == "social"
    assert understood("Merci Jeli !", "fr").reply == TEXTS["fr"]["thanks_reply"]
    assert understood("Source ?").kind == "sources"
    assert understood("Who are you?").kind == "about_jeli"


def test_without_a_model_the_old_rules_still_sort_messages():
    assert plain("What did I miss since Monday?").kind == "catchup"
    assert plain("Quelles sont les prochaines échéances ?").kind == "deadlines"
    assert plain("Summary of the Module 1 session").kind == "recap"
    assert plain("Envoie-moi le guide").kind == "file"
    assert plain("When is the bootcamp?").kind == "question"


class ModelUnderstands:
    """The model's answer, as understand.py receives it."""

    def __init__(self, result):
        self.result, self.prompts = result, []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return self.result


def test_the_model_gets_the_brief_the_state_and_the_member_and_its_language_wins():
    understander = Understander(ModelUnderstands(Understood(kind="catchup", since="2026-09-21T00:00:00Z", language="fr")))
    understander.brief = "Wadhwani Ignite: the entrepreneurship programme."

    async def state():
        return "Sessions Jeli has transcribed: Module 1."

    understander.state = state
    result = asyncio.run(understander.understand("recap d'aujourd'hui", "en", member="Name shown on WhatsApp: Awa."))
    assert result.kind == "catchup" and result.language == "fr" and result.since.startswith("2026-09-21")
    prompt = understander.llm.prompts[0]
    assert "Wadhwani Ignite" in prompt and "Module 1" in prompt and "Awa" in prompt


class FakeStore:
    def __init__(self):
        self.turns_saved, self.members = [], {}
        self.rows = []

    async def add_turn(self, chat_id, member_key, is_private, message, reply, sources):
        self.turns_saved.append((chat_id, member_key, message, reply, sources))

    async def turns(self, chat_id, member_key, since, limit=8):
        return self.rows

    async def forget_turns(self, member_key):
        self.rows = []

    async def member(self, member_key):
        return self.members.get(member_key)

    async def remember_member(self, member_key, name="", language="", notes=None):
        self.members[member_key] = {"name": name, "language": language, "notes": notes or {}}

    async def list_documents(self):
        return [SimpleNamespace(title="Hackathon guidelines", pages=1)]


def test_a_conversation_survives_a_restart():
    store = FakeStore()
    now = datetime.now(timezone.utc)
    store.rows = [{"at": now - timedelta(minutes=3), "message": "/recap", "reply": "Which session? 1. A 2. B", "sources": []}]
    conversations = Conversations(store=store)
    asyncio.run(conversations.warm(message("2", addressed=False)))
    assert [t.message for t in conversations.history(message("2", addressed=False))] == ["/recap"]
    # Jeli asked a question: a short answer continues the conversation without "Jeli".
    assert conversations.is_follow_up(message("2", addressed=False))
    assert not conversations.is_follow_up(message("2", addressed=False, author_id="22399999999@c.us"))


def test_replies_are_written_to_the_store_with_their_sources():
    store = FakeStore()

    async def run():
        conversations = Conversations(store=store)
        conversations.note(message("when?"), Reply("Thursday.", cited=["> *Diane*, Thu 17 Sep"]), ["> *Diane*, Thu 17 Sep"])
        await asyncio.sleep(0)

    asyncio.run(run())
    assert store.turns_saved == [("g@g.us", "22370000000", "when?", "Thursday.", ["> *Diane*, Thu 17 Sep"])]


class Answers:
    async def answer(self, question, asker, **context):
        return Reply("answer", cited=["> *Diane*, Thu 17 Sep"])

    async def prefetch(self, text):
        return []


def test_source_and_voice_requests_use_the_previous_answer():
    store = FakeStore()
    responder = Responder(Answers(), store=store, understander=Understander(None))
    asyncio.run(responder.respond(message("When is the deadline?")))
    assert asyncio.run(responder.respond(message("source?"))) == "> *Diane*, Thu 17 Sep"
    understands_voice = Understander(ModelUnderstands(Understood(kind="voice", language="en")))
    responder.understander = understands_voice
    assert asyncio.run(responder.respond(message("en vocal"))) == "> *Diane*, Thu 17 Sep"  # the last reply, said again


def test_listing_documents_and_remembering_the_member():
    store = FakeStore()
    responder = Responder(Answers(), store=store, understander=Understander(ModelUnderstands(Understood(kind="list_documents", language="fr"))))
    reply = asyncio.run(responder.respond(message("Combien de documents as-tu ?")))
    assert reply.startswith(TEXTS["fr"]["documents_header"].format(n=1)) and "Hackathon guidelines" in reply
    asyncio.run(asyncio.sleep(0))
    assert store.members["22370000000"]["language"] == "fr" and store.members["22370000000"]["name"] == "Awa"


def test_a_clarifying_question_is_what_the_model_wrote():
    responder = Responder(Answers(), understander=Understander(ModelUnderstands(
        Understood(kind="clarify", reply="Tu parles de la session MIT du 16 ou du coaching Wadhwani du 17 ?", language="fr"))))
    assert asyncio.run(responder.respond(message("C'était quand ça ?"))) == "Tu parles de la session MIT du 16 ou du coaching Wadhwani du 17 ?"


def test_sources_must_share_something_with_the_answer():
    assert supports("The bootcamp moves to 25 September.", "It moved to 25 September.")
    assert supports("Build phase: Friday 18 to Thursday 24 September.", "Le build phase se termine jeudi 24 septembre.")
    assert not supports("Peace and blessings, family!", "Yes, I can answer in Afrikaans.")
    assert parse_source("3.2") == (3, 2) and parse_source("[3]") == (3, None) and parse_source("x") is None


def test_the_background_block_is_built_from_what_is_known():
    assert background() == ""
    text = background("Brief.", "State.", "Member.")
    assert text.startswith("What Jeli knows about the community") and "Jeli's own state:\nState." in text and text.endswith("Member.")


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes, self.calls = list(outcomes), []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed=outcome, text=outcome.model_dump_json())


def test_a_bad_moment_costs_at_most_a_few_attempts_not_every_key():
    clients = [SimpleNamespace(aio=SimpleNamespace(models=None)) for _ in range(15)]
    models = FakeModels([errors.ServerError(503, {"error": {}})] * 40)
    for client in clients:
        client.aio.models = models
    llm = LLM("unused", ["best", "next", "lite"], client=clients[0])
    llm._clients = clients
    try:
        asyncio.run(llm.answer("system", "prompt"))
    except Exception as error:
        assert type(error).__name__ == "LLMUnavailable"
    assert len(models.calls) == MAX_ATTEMPTS
    assert models.calls == ["best", "best", "next", "next"]  # two keys of the best model, then the next model


def test_pairs_in_cooldown_are_skipped_while_others_are_fresh():
    good = GeneratedAnswer(answered=True, answer="ok", sources=["1"])
    clock = SimpleNamespace(now=0.0)
    models = FakeModels([errors.ClientError(429, {"error": {}}), good, good])
    llm = LLM("unused", ["best", "next"], client=SimpleNamespace(aio=SimpleNamespace(models=models)), clock=lambda: clock.now)
    asyncio.run(llm.answer("s", "p"))  # best: quota → rests; next answers
    asyncio.run(llm.answer("s", "p"))  # straight to next: the resting pair is not tried
    assert models.calls == ["best", "next", "next"]
