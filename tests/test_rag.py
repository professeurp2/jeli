import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.genai import errors

from app.answer.citations import display_author
from app.answer.language import TEXTS, detect_language
from app.answer.llm import LLM, GeneratedAnswer, LLMUnavailable
from app.answer.rag import Answerer
from app.answer.citations import timestamped_link
from app.kb.store import SearchHit
from app.models import Recording, StoredMessage

T0 = datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc)
OTHER_BOT = "+229 01 49 48 62 56"

MESSAGES = [
    StoredMessage("m1", "meti", "whatsapp_export", "Awa Traoré", T0, "The bootcamp moves to 25 September."),
    StoredMessage("m2", "meti", "whatsapp_export", "+234 818 554 6555", T0 + timedelta(minutes=2), "Same venue?"),
    StoredMessage("m3", "meti", "whatsapp_export", OTHER_BOT, T0 + timedelta(minutes=3), "I am a bot: it is in Addis."),
    StoredMessage("m4", "meti", "whatsapp_export", "Moussa", T0 + timedelta(days=1), "Pitch deck due Friday 6 pm."),
]


def hit(chunk_id, ids, similarity, started_at):
    return SearchHit(chunk_id, "meti", started_at, started_at, [], list(ids), "", 0.0, similarity)


# As search returns them: most relevant first (here the later conversation), not in time order.
HITS = [hit(2, ["m4"], 0.74, T0 + timedelta(days=1)), hit(1, ["m1", "m2", "m3"], 0.70, T0)]


RECORDING = Recording(
    id="recording:module-1", title="Module 1 class session", recorded_at=datetime(2026, 9, 15, 13, 0, tzinfo=timezone.utc),
    method="gemini", source_url="https://youtu.be/6q4uPBO_sDc",
)
MESSAGES.append(
    StoredMessage("r1", RECORDING.id, "recording", "Charles Botom", RECORDING.recorded_at + timedelta(minutes=12, seconds=34),
                  "Every team member needs to complete the course.")
)


class FakeStore:
    async def messages_by_ids(self, ids):
        return [m for m in MESSAGES if m.id in ids]

    async def recordings(self, ids):
        return {RECORDING.id: RECORDING} if RECORDING.id in ids else {}


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.prompts = result, error, []

    async def answer(self, system, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


def make_answerer(llm, hits=HITS, monkeypatch=None):
    async def fake_search(store, embedder, question, limit):
        return hits

    monkeypatch.setattr("app.answer.rag.search", fake_search)
    return Answerer(
        FakeStore(), embedder=None, llm=llm, min_similarity=0.6,
        ignored_authors=[OTHER_BOT], chat_labels={"meti": "METI cohort"},
    )


def ask(answerer, question="When is the bootcamp?"):
    return asyncio.run(answerer.answer(question, asker="+223 70 00 00 00"))


def test_grounded_answer_cites_only_the_sources_it_used(monkeypatch):
    llm = FakeLLM(GeneratedAnswer(answered=True, answer="It moved to 25 September.", sources=[1]))
    reply = ask(make_answerer(llm, monkeypatch=monkeypatch))
    assert reply.startswith("It moved to 25 September.")
    assert "📌 Sources\n[1] METI cohort · 12 Sep 2026, 14:00 UTC · Awa Traoré, +234 ···55" in reply
    assert "[2]" not in reply


def test_the_model_sees_chronological_excerpts_without_bots_or_full_phone_numbers(monkeypatch):
    llm = FakeLLM(GeneratedAnswer(answered=True, answer="ok", sources=[1]))
    ask(make_answerer(llm, monkeypatch=monkeypatch))
    [prompt] = llm.prompts
    assert prompt.index("[1] METI cohort") < prompt.index("[2] METI cohort")
    assert "bootcamp moves" in prompt and "Pitch deck" in prompt
    assert "I am a bot" not in prompt
    assert "818 554 6555" not in prompt and "70 00 00 00" not in prompt


def test_unrelated_questions_get_i_dont_know_without_calling_the_model(monkeypatch):
    llm = FakeLLM()
    answerer = make_answerer(llm, hits=[hit(1, ["m1"], 0.55, T0)], monkeypatch=monkeypatch)
    assert ask(answerer, "What is the capital of Japan?") == TEXTS["en"]["dont_know"]
    assert ask(answerer, "Quelle est la capitale du Japon ?") == TEXTS["fr"]["dont_know"]
    assert llm.prompts == []


@pytest.mark.parametrize(
    "generated",
    [
        GeneratedAnswer(answered=False, answer="Not in the excerpts.", sources=[]),
        GeneratedAnswer(answered=True, answer="Tokyo.", sources=[]),  # no source: not grounded
        GeneratedAnswer(answered=True, answer="Tokyo.", sources=[9]),  # cites an excerpt that doesn't exist
    ],
)
def test_answers_without_real_sources_become_i_dont_know(monkeypatch, generated):
    assert ask(make_answerer(FakeLLM(generated), monkeypatch=monkeypatch)) == TEXTS["en"]["dont_know"]


def test_when_no_model_is_available_jeli_points_to_the_most_relevant_sources(monkeypatch):
    reply = ask(make_answerer(FakeLLM(error=LLMUnavailable()), monkeypatch=monkeypatch))
    assert reply.startswith(TEXTS["en"]["fallback"])
    # The most relevant excerpt comes first, even though it is the more recent one.
    assert reply.index("13 Sep 2026") < reply.index("12 Sep 2026")
    assert "« Moussa: Pitch deck due Friday 6 pm. »" in reply


def test_answers_from_a_call_link_to_the_moment_it_was_said(monkeypatch):
    recording_hit = SearchHit(9, RECORDING.id, MESSAGES[-1].sent_at, MESSAGES[-1].sent_at, [], ["r1"], "", 0.0, 0.8)
    llm = FakeLLM(GeneratedAnswer(answered=True, answer="Everyone in the team must complete it.", sources=[1]))
    reply = ask(make_answerer(llm, hits=[recording_hit], monkeypatch=monkeypatch), "Must every member do the course?")
    [prompt] = llm.prompts
    assert "Call recording «Module 1 class session» (15 September 2026), from 12:34" in prompt
    assert "[12:34] Charles Botom: Every team member" in prompt
    assert "[1] 🎥 Module 1 class session · 15 Sep 2026 · at 12:34 · Charles Botom" in reply
    assert "https://youtu.be/6q4uPBO_sDc?t=754" in reply


def test_timestamped_links_only_for_youtube():
    offset = timedelta(minutes=1, seconds=5)
    assert timestamped_link("https://www.youtube.com/watch?v=abcdefgh&t=10", offset) == "https://www.youtube.com/watch?v=abcdefgh&t=65"
    assert timestamped_link("https://drive.google.com/file/d/xyz/view", offset) is None
    assert timestamped_link(None, offset) is None


def test_phone_numbers_are_masked_but_names_kept():
    assert display_author("+234 818 554 6555") == "+234 ···55"
    assert display_author("22370000000") == "···00"
    assert display_author("Awa Traoré") == "Awa Traoré"


def test_language_detection():
    assert detect_language("Quand aura lieu le bootcamp ?") == "fr"
    assert detect_language("Où est la réunion") == "fr"
    assert detect_language("When is the deadline?") == "en"


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes, self.models = list(outcomes), []

    async def generate_content(self, model, contents, config):
        self.models.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(parsed=outcome, text=outcome.model_dump_json())


def make_llm(outcomes):
    models = FakeModels(outcomes)
    llm = LLM("unused", ["primary", "backup"], client=SimpleNamespace(aio=SimpleNamespace(models=models)))
    return llm, models


def test_llm_falls_back_to_the_next_model_on_quota_or_overload():
    good = GeneratedAnswer(answered=True, answer="ok", sources=[1])
    llm, models = make_llm([errors.ClientError(429, {"error": {"message": "quota"}}), good])
    assert asyncio.run(llm.answer("system", "prompt")) == good
    assert models.models == ["primary", "backup"]

    llm, models = make_llm([errors.ServerError(503, {"error": {"message": "busy"}}), good])
    assert asyncio.run(llm.answer("system", "prompt")) == good


def test_a_model_out_of_quota_is_skipped_for_a_while():
    good = GeneratedAnswer(answered=True, answer="ok", sources=[1])
    clock = SimpleNamespace(now=0.0)
    models = FakeModels([errors.ClientError(429, {"error": {}}), good, good, good])
    llm = LLM("unused", ["primary", "backup"], client=SimpleNamespace(aio=SimpleNamespace(models=models)),
              clock=lambda: clock.now)
    asyncio.run(llm.answer("system", "prompt"))
    asyncio.run(llm.answer("system", "prompt"))  # primary is resting: straight to the backup
    assert models.models == ["primary", "backup", "backup"]
    clock.now += 301
    asyncio.run(llm.answer("system", "prompt"))  # rested: primary again
    assert models.models[-1] == "primary"


def test_llm_gives_up_when_every_model_fails():
    llm, _ = make_llm([errors.ClientError(429, {"error": {}}), errors.ServerError(503, {"error": {}})])
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.answer("system", "prompt"))


def test_llm_does_not_hide_request_bugs():
    llm, models = make_llm([errors.ClientError(400, {"error": {"message": "bad request"}})])
    with pytest.raises(errors.ClientError):
        asyncio.run(llm.answer("system", "prompt"))
    assert models.models == ["primary"]
