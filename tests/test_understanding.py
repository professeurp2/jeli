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


def test_an_answer_claimed_from_the_brief_must_be_in_the_brief():
    from app.answer.rag import from_brief

    brief = "The programme is funded by METI Japan and run with UNDP timbuktoo. Wadhwani Ignite: the 14-week entrepreneurship programme."
    assert from_brief(brief, "What is Wadhwani Ignite?", "Wadhwani Ignite is the entrepreneurship programme.")
    assert not from_brief(brief, "What is the capital of Japan?", "Tokyo is the capital of Japan.")


def test_citation_ids_never_leak_into_the_answer_text():
    from app.answer.rag import clean_answer, clean_title

    assert clean_answer("Teams can have up to 5 members, as stated in the guidelines [3.1].") == "Teams can have up to 5 members, as stated in the guidelines."
    assert clean_answer("Together on their own dashboards [1.1, 3.6, 3.7]. Only one person inputs [2.2].") == "Together on their own dashboards. Only one person inputs."
    assert clean_answer("She reminded everyone *[3.1].*") == "She reminded everyone."
    assert clean_title("📎 *Hackathon Guidelines*") == "Hackathon Guidelines"


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
    assert len(models.calls) <= MAX_ATTEMPTS
    assert models.calls == ["best", "next", "lite"]  # an overloaded model is so on every key: the next model at once


def test_pairs_in_cooldown_are_skipped_while_others_are_fresh():
    good = GeneratedAnswer(answered=True, answer="ok", sources=["1"])
    clock = SimpleNamespace(now=0.0)
    models = FakeModels([errors.ClientError(429, {"error": {}}), good, good])
    llm = LLM("unused", ["best", "next"], client=SimpleNamespace(aio=SimpleNamespace(models=models)), clock=lambda: clock.now)
    asyncio.run(llm.answer("s", "p"))  # best: quota → rests; next answers
    asyncio.run(llm.answer("s", "p"))  # straight to next: the resting pair is not tried
    assert models.calls == ["best", "next", "next"]


def test_a_model_out_of_its_daily_quota_everywhere_does_not_stop_the_next_model():
    good = GeneratedAnswer(answered=True, answer="ok", sources=["1"])
    daily = errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
                                               "details": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}})

    class ByModel:
        calls = []

        async def generate_content(self, model, contents, config):
            self.calls.append(model)
            if model == "best":
                raise daily  # spent for the day, on every key
            return SimpleNamespace(parsed=good, text=good.model_dump_json())

    models = ByModel()
    clients = [SimpleNamespace(aio=SimpleNamespace(models=models)) for _ in range(4)]
    clock = SimpleNamespace(now=0.0)
    llm = LLM("unused", ["best", "lite"], client=clients[0], clock=lambda: clock.now)
    llm._clients = clients
    # Only two attempts, as the optional steps ask: the refusals cost none of them.
    assert asyncio.run(llm.generate("p", GeneratedAnswer, attempts=2)) == good
    assert models.calls == ["best", "best", "best", "best", "lite"]  # every key of the best model, then the next one
    clock.now += 3600  # an hour later: the day's quota is still spent on those keys
    for _ in range(3):
        asyncio.run(llm.generate("p", GeneratedAnswer, attempts=2))
    assert models.calls.count("best") <= 4  # each key refused once a day at most, then left alone


def test_the_daily_quota_renews_at_midnight_pacific_time():
    from datetime import datetime, timezone

    from app.answer.llm import quota_day, quota_renewal, seconds_until_quota_renewal

    summer = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert quota_day(summer).isoformat() == "2026-09-22" and quota_renewal(summer) == datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)
    assert seconds_until_quota_renewal(summer) == 19 * 3600
    winter = datetime(2026, 12, 1, 7, 30, tzinfo=timezone.utc)
    assert quota_day(winter).isoformat() == "2026-11-30" and quota_renewal(winter) == datetime(2026, 12, 1, 8, 0, tzinfo=timezone.utc)


def test_a_key_out_of_quota_hands_over_to_the_same_model_on_the_next_key():
    good = GeneratedAnswer(answered=True, answer="ok", sources=["1"])
    daily = errors.ClientError(429, {"error": {"code": 429, "message": "PerDay quota", "status": "RESOURCE_EXHAUSTED"}})
    overloaded = errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})

    class Keys:
        def __init__(self, behaviour):
            self.behaviour, self.calls = behaviour, calls

        async def generate_content(self, model, contents, config):
            self.calls.append((self.behaviour, model))
            if model != "best":
                raise overloaded  # the lite models are overloaded
            if self.behaviour == "spent":
                raise daily
            return SimpleNamespace(parsed=good, text=good.model_dump_json())

    calls = []
    clients = [SimpleNamespace(aio=SimpleNamespace(models=Keys(b))) for b in ("spent", "spent", "spent", "fresh")]
    llm = LLM("unused", ["best", "lite"], client=clients[0])
    llm._clients = clients
    assert asyncio.run(llm.generate("p", GeneratedAnswer, attempts=2)) == good
    assert calls == [("spent", "best"), ("spent", "best"), ("spent", "best"), ("fresh", "best")]  # never the overloaded lite


def test_the_light_models_share_the_keys_and_what_is_known_of_them():
    llm = LLM(["k1", "k2", "k3"], ["gemini-3.6-flash", "gemini-3.5-flash-lite"])
    light = llm.with_models(["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"])
    assert light.models == ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] and llm.models[0] == "gemini-3.6-flash"
    assert light._clients is llm._clients and light._api_keys is llm._api_keys
    llm._rest(0, "gemini-3.5-flash-lite", "quota", seconds=3600)  # found spent while answering…
    assert (0, "gemini-3.5-flash-lite") not in light._build_pairs()  # …not asked again for the light tasks
    light._disable_key(2)
    assert llm.valid_keys() == [0, 1]  # a key Google refuses is refused everywhere


def test_settings_give_the_light_models_most_of_the_work():
    from app.config import Settings

    settings = Settings(_env_file=None)
    assert settings.answer_models[0] == "gemini-3.6-flash"  # the answers members read
    # The cheap models do the work: the best one is last, reached only when they are all down
    # (23 Sep at 17:30, they were — and every call fell through to the spare engine instead).
    assert settings.light_model_list[0] != "gemini-3.6-flash"
    assert settings.light_model_list[-1] == "gemini-3.6-flash"
    assert settings.transcription_model_list[0] == "gemini-3.5-flash-lite"


def test_an_overloaded_model_hands_over_to_the_next_model_not_the_next_key():
    good = GeneratedAnswer(answered=True, answer="ok", sources=["1"])

    class Models:
        calls = []

        async def generate_content(self, model, contents, config):
            self.calls.append(model)
            if model == "lite-a":
                raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})
            return SimpleNamespace(parsed=good, text=good.model_dump_json())

    models = Models()
    clients = [SimpleNamespace(aio=SimpleNamespace(models=models)) for _ in range(5)]
    llm = LLM("unused", ["lite-a", "lite-b"], client=clients[0])
    llm._clients = clients
    assert asyncio.run(llm.generate("p", GeneratedAnswer, attempts=2)) == good
    assert models.calls == ["lite-a", "lite-b"]  # not "lite-a" again on another key


def test_the_understanding_step_knows_someone_saying_who_they_are():
    """Jeli knows members by an account id. Being told a name and a number is how it learns the
    rest — and it has to recognise it in any words, not after a question it asked."""
    from app.answer.understand import KINDS, SYSTEM, Understood

    assert "identity" in KINDS
    assert '"identity": tells Jeli who they are' in SYSTEM
    assert "whether Jeli\n    asked for them or they simply said so" in SYSTEM
    # Someone else's contact, or a number quoted for another reason, is not this.
    assert "is not\n    this" in SYSTEM
    assert Understood(kind="identity").person_name == "" and Understood(kind="identity").person_number == ""
