"""The spare engine: it answers when Gemini has nothing left, and only for text."""

import asyncio

import httpx
import pytest
from pydantic import BaseModel

from app.answer.groq import BackupUnavailable, Groq


class Shape(BaseModel):
    answered: bool
    answer: str


def engine(handler, models=("first", "second"), key="k") -> Groq:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Groq(key, list(models), clock=lambda: 0.0, client=client)


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def model_of(request: httpx.Request) -> str:
    import json

    return json.loads(request.read())["model"]


def test_answers_a_text_prompt():
    asked = {}

    def handler(request: httpx.Request) -> httpx.Response:
        asked["body"] = request.read().decode()
        return reply('{"answered": true, "answer": "Friday at 10"}')

    result = asyncio.run(engine(handler).generate("When is the meeting?", Shape, system="Be short"))
    assert result.answer == "Friday at 10"
    # The shape is asked for in words, since Groq has no response schema.
    assert "Be short" in asked["body"] and "json" in asked["body"].lower() and "answered" in asked["body"]


def test_a_refused_model_rests_and_the_next_one_answers():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = model_of(request)
        seen.append(model)
        return httpx.Response(429) if model == "first" else reply('{"answered": true, "answer": "ok"}')

    made = engine(handler)
    assert asyncio.run(made.generate("hello", Shape)).answer == "ok"
    assert seen == ["first", "second"]
    # The refused model is left alone: the next call goes straight to the one that works.
    seen.clear()
    asyncio.run(made.generate("hello", Shape))
    assert seen == ["second"]


def test_recordings_and_pictures_stay_on_gemini():
    made = engine(lambda request: reply("{}"))
    with pytest.raises(BackupUnavailable):
        asyncio.run(made.generate([{"inline_data": b"audio"}], Shape))


def test_without_a_key_it_is_off():
    made = Groq("", ["first"])
    assert not made.available
    with pytest.raises(BackupUnavailable):
        asyncio.run(made.generate("hello", Shape))


def test_unusable_output_is_not_passed_on_as_an_answer():
    made = engine(lambda request: reply("I cannot do that"), models=("first",))
    with pytest.raises(BackupUnavailable):
        asyncio.run(made.generate("hello", Shape))


def gemini_all_failing(backup):
    """A client whose every model answers "high demand", like Google on 22 Sep at 16:48."""
    from types import SimpleNamespace

    from google.genai import errors

    from app.answer.llm import LLM

    class Models:
        async def generate_content(self, model, contents, config):
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})

    client = SimpleNamespace(aio=SimpleNamespace(models=Models()))
    return LLM("unused", ["first", "second"], client=client, backup=backup)


def test_when_every_gemini_model_refuses_the_spare_engine_answers():
    from app.answer.llm import GeneratedAnswer, LLMUnavailable

    made = engine(lambda request: reply('{"answered": true, "answer": "Friday", "sources": []}'))
    llm = gemini_all_failing(made)
    assert asyncio.run(llm.answer("s", "When is the meeting?")).answer == "Friday"
    assert made.used == 1
    # Without a spare engine, or when it fails too, Jeli still says it cannot answer.
    with pytest.raises(LLMUnavailable):
        asyncio.run(gemini_all_failing(None).generate("p", GeneratedAnswer))
    with pytest.raises(LLMUnavailable):
        asyncio.run(gemini_all_failing(Groq("", ["first"])).generate("p", GeneratedAnswer))


def test_the_spare_engine_is_shared_by_the_light_models():
    made = engine(lambda request: reply('{"answered": true, "answer": "ok", "sources": []}'))
    assert gemini_all_failing(made).with_models(["lite"]).backup is made


def test_jeli_runs_on_the_spare_engine_alone_when_no_gemini_key_is_left():
    """Removing every Gemini key must not crash Jeli: the spare engine carries the text answers."""
    from app.answer.llm import LLM

    made = engine(lambda request: reply('{"answered": true, "answer": "Friday", "sources": []}'))
    llm = LLM([], ["first"], backup=made)
    assert llm.client is None and llm.key_count == 0  # nothing to call at Google, nothing to check
    assert all(resting for _, resting in llm.status())  # the dashboard shows every model out
    assert asyncio.run(llm.answer("s", "When is the meeting?")).answer == "Friday"
