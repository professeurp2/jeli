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


def test_the_spare_engine_proves_itself_at_startup():
    """The team must not discover the key is wrong the day Gemini goes down."""
    working = engine(lambda request: reply('{"answered": true, "answer": "ok"}'), models=("first",))
    assert asyncio.run(working.check()) == "ok" and working.checked == "ok"
    broken = engine(lambda request: httpx.Response(401), models=("first",))
    assert asyncio.run(broken.check()) == "BackupUnavailable"  # said on /health and the dashboard
    assert Groq("", ["first"]).checked == ""  # off: nothing claimed either way


def test_whisper_listens_when_no_gemini_model_can():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/audio/transcriptions")
        body = request.read()
        assert b"whisper" in body and b"voice.ogg" in body
        return httpx.Response(200, json={"text": "  C'est quand la reunion ?  "})

    made = engine(handler, models=("first",))
    assert asyncio.run(made.hear(b"audio-bytes")) == "C'est quand la reunion ?"
    assert made.heard == 1
    assert asyncio.run(engine(lambda r: httpx.Response(500)).hear(b"audio")) is None
    assert asyncio.run(Groq("", ["first"]).hear(b"audio")) is None


def test_a_retired_model_id_is_replaced_by_what_the_key_really_has():
    """Measured 22 Sep: Groq answered 404 on the configured name. Jeli asks rather than insists."""
    listing = {"data": [{"id": "whisper-large-v3"}, {"id": "whisper-large-v3-turbo"},
                        {"id": "llama-3.1-8b-instant"}, {"id": "llama-3.3-70b-versatile-0925"},
                        {"id": "meta-llama/llama-guard-4-12b"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=listing)
        return reply('{"answered": true, "answer": "ok"}')

    made = engine(handler, models=("gone-for-good",))
    assert asyncio.run(made.check()) == "ok"
    # The dated variant of the preferred model wins; the moderation model is never for answers.
    assert made.models == ["llama-3.3-70b-versatile-0925", "llama-3.1-8b-instant"]
    assert made.hear_model == "whisper-large-v3-turbo"
    assert made.used == 0  # the test calls are not answers it rescued


def test_a_model_that_does_not_answer_the_test_is_dropped_whatever_it_is_called():
    """Measured 22 Sep: a key with no llama at all had a voice model picked as the spare engine."""
    listing = {"data": [{"id": "canopylabs/orpheus-arabic-saudi"}, {"id": "qwen/qwen3.8-27b"},
                        {"id": "llama-4-scout"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=listing)
        model = model_of(request)
        # Only one of them really holds a conversation.
        return reply('{"answered": true, "answer": "ok"}') if model == "qwen/qwen3.8-27b" else httpx.Response(400)

    made = engine(handler, models=("gone-for-good",))
    assert asyncio.run(made.check()) == "ok"
    assert made.models == ["qwen/qwen3.8-27b"]  # the voice model was never a candidate


def test_when_nothing_answers_the_dashboard_is_told():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "llama-4-scout"}]})
        return httpx.Response(401)

    made = engine(handler, models=("first",))
    assert asyncio.run(made.check()) == "BackupUnavailable"


def test_a_configured_model_the_key_has_is_kept():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "first"}, {"id": "llama-3.1-8b-instant"}]})
        return reply('{"answered": true, "answer": "ok"}')

    made = engine(handler, models=("first",))
    asyncio.run(made.check())
    assert made.models == ["first"]


def test_the_spare_engine_is_reached_sooner_than_a_fourth_slow_failure():
    """The audit of 22 Sep: four overloaded models cost ~24 s before Groq was asked."""
    from app.answer.llm import ATTEMPTS_BEFORE_BACKUP, GeneratedAnswer

    tried = []

    def gemini_overloaded():
        from types import SimpleNamespace

        from google.genai import errors

        class Models:
            async def generate_content(self, model, contents, config):
                tried.append(model)
                raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})

        return SimpleNamespace(aio=SimpleNamespace(models=Models()))

    from app.answer.llm import LLM

    made = engine(lambda request: reply('{"answered": true, "answer": "ok", "sources": []}'))
    llm = LLM("unused", ["a", "b", "c", "d"], client=gemini_overloaded(), backup=made)
    assert asyncio.run(llm.answer("s", "text question")).answer == "ok"
    assert len(tried) == ATTEMPTS_BEFORE_BACKUP  # not the full four
    # A prompt Groq cannot take (a recording) still gets Gemini's full run.
    tried.clear()
    fresh = LLM("unused", ["a", "b", "c", "d"], client=gemini_overloaded(), backup=made)
    with pytest.raises(Exception):
        asyncio.run(fresh.generate([{"inline_data": b"audio"}], GeneratedAnswer))
    assert len(tried) > ATTEMPTS_BEFORE_BACKUP


def test_the_spare_engine_gets_the_time_the_caller_allowed():
    """Measured 22 Sep at 23:54: a catch-up over 735 messages is given 30 s, and the spare engine
    was cut off at its own default of 8 s — no model writes that digest in 8 s."""
    from types import SimpleNamespace

    from google.genai import errors

    from app.answer.llm import LLM

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout", {}).get("read")
        return reply('{"answered": true, "answer": "ok", "sources": []}')

    class Models:
        async def generate_content(self, model, contents, config):
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})

    made = engine(handler, models=("first",))
    llm = LLM("unused", ["a"], client=SimpleNamespace(aio=SimpleNamespace(models=Models())), backup=made)
    from app.answer.llm import GeneratedAnswer

    asyncio.run(llm.generate("a long catch-up prompt", GeneratedAnswer, timeout=30))
    assert seen["timeout"] == 30


def test_a_source_too_big_is_shortened_until_it_fits_whatever_the_feature():
    """Measured 23 Sep at 00:13: a session recap was 63,582 characters and the spare engine
    refused it. A catch-up, a recap or a document must not each need their own patch."""
    import json

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        said = body["messages"][1]["content"]
        seen.append((len(said), body["messages"][0]["content"]))
        if len(said) > 20_000:
            return httpx.Response(413, json={"error": {"message": "Request too large for model"}})
        return reply('{"answered": true, "answer": "ok"}')

    made = engine(handler, models=("first",))
    assert asyncio.run(made.generate("x" * 80_000, Shape)).answer == "ok"
    assert [length for length, _ in seen] == [80_000, 40_000, 20_000]  # halved until accepted, exactly
    # It was told it is reading only part, so its answer says so — whatever the caller asked for.
    assert "only part" in seen[-1][1]
    assert "only part" not in seen[0][1]  # nothing claimed while it had the whole thing


def test_shortening_keeps_the_beginning_and_the_end():
    from app.answer.groq import _shorten

    text = "START" + ("m" * 1000) + "END"
    short = _shorten(text, 200)
    assert short.startswith("START") and short.endswith("END") and len(short) < len(text)
    assert "are missing" in short  # the gap is marked where it happens
    assert _shorten("short enough", 500) == "short enough"


def test_a_model_failing_for_its_own_reasons_is_not_shortened_at():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(500, json={"error": {"message": "internal"}})

    made = engine(handler, models=("first",))
    with pytest.raises(BackupUnavailable):
        asyncio.run(made.generate("x" * 80_000, Shape))
    assert len(calls) == 1  # tried once, rested — shortening would not have helped


def _both(handler=None):
    """Gemini that answers, and a spare engine that answers: to see which one is asked."""
    from types import SimpleNamespace

    from app.answer.llm import GeneratedAnswer, LLM

    gemini = GeneratedAnswer(answered=True, answer="from Gemini", sources=[])
    asked = []

    class Models:
        async def generate_content(self, model, contents, config):
            asked.append("gemini")
            return SimpleNamespace(parsed=gemini, text=gemini.model_dump_json())

    def groq_handler(request):
        asked.append("spare")
        return reply('{"answered": true, "answer": "from the spare engine", "sources": []}')

    made = engine(handler or groq_handler, models=("first",))
    llm = LLM("unused", ["a"], client=SimpleNamespace(aio=SimpleNamespace(models=Models())), backup=made)
    return llm, asked


def test_the_team_chooses_which_engine_answers():
    llm, asked = _both()
    # Auto: Gemini first, the spare engine only when it has nothing left.
    assert asyncio.run(llm.answer("s", "q")).answer == "from Gemini"
    assert asked == ["gemini"]
    # The spare engine first, to try it or to spare the day's Gemini quota.
    asked.clear()
    llm.engine = "backup"
    assert asyncio.run(llm.answer("s", "q")).answer == "from the spare engine"
    assert asked == ["spare"]
    # Gemini only: the spare engine is never asked, even when Gemini fails.
    asked.clear()
    llm.engine = "gemini_only"
    assert asyncio.run(llm.answer("s", "q")).answer == "from Gemini"
    assert asked == ["gemini"]


def test_a_choice_that_leaves_no_way_out_is_honoured():
    from app.answer.llm import LLMUnavailable

    llm, asked = _both(lambda request: httpx.Response(500))
    llm.engine = "backup_only"
    with pytest.raises(LLMUnavailable):
        asyncio.run(llm.answer("s", "q"))
    assert "gemini" not in asked  # Gemini was switched off: it is not used behind the team's back
    # With "backup", the same failure falls back to Gemini instead.
    llm.engine = "backup"
    assert asyncio.run(llm.answer("s", "q")).answer == "from Gemini"


def test_every_tier_switches_at_the_same_moment():
    llm, _ = _both()
    light = llm.with_models(["lite"])
    llm.engine = "backup_only"
    assert light.engine == "backup_only"
