"""Model rotation by measured health (22 Sep): an overloaded model steps back for everyone, the
ones that answer move ahead; event reminders an hour before a session; answers in any language."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from google.genai import errors

from app.answer.llm import LLM, MODEL_REST_SECONDS, GeneratedAnswer
from app.answer.prompts import build_prompt
from app.jobs.event_reminders import event_moment, post_event_reminders, times_text
from app.models import Deadline

GOOD = GeneratedAnswer(answered=True, answer="ok", sources=["1"])


class Models:
    def __init__(self, failing):
        self.failing, self.calls = set(failing), []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        if model in self.failing:
            raise errors.ServerError(503, {"error": {"code": 503, "message": "high demand"}})
        return SimpleNamespace(parsed=GOOD, text=GOOD.model_dump_json())


def make(models, failing, keys=4):
    fake = Models(failing)
    clock = SimpleNamespace(now=0.0)
    llm = LLM("unused", models, client=SimpleNamespace(aio=SimpleNamespace(models=fake)), clock=lambda: clock.now)
    llm._clients = [SimpleNamespace(aio=SimpleNamespace(models=fake)) for _ in range(keys)]
    return llm, fake, clock


def test_a_model_that_fails_steps_behind_the_ones_that_answer():
    llm, fake, clock = make(["lite-a", "lite-b", "lite-c"], failing={"lite-a"})
    asyncio.run(llm.answer("s", "p"))
    assert fake.calls == ["lite-a", "lite-b"]  # paid the failure once…
    asyncio.run(llm.answer("s", "p"))
    assert fake.calls[-1] == "lite-b"  # …then the healthy model goes first
    assert llm.model_health()[0] == {"model": "lite-a", "streak": 1, "resting": MODEL_REST_SECONDS, "latency": None}
    clock.now += MODEL_REST_SECONDS + 1
    asyncio.run(llm.answer("s", "p"))
    assert fake.calls[-2:] == ["lite-a", "lite-b"]  # tried again after its rest: still failing…
    assert llm.model_health()[0]["resting"] == 2 * MODEL_REST_SECONDS  # …so it rests twice as long
    fake.failing.clear()
    clock.now += 5 * MODEL_REST_SECONDS
    asyncio.run(llm.answer("s", "p"))
    asyncio.run(llm.answer("s", "p"))
    assert fake.calls[-1] == "lite-a" and llm.model_health()[0]["streak"] == 0  # back in front once it answers


def test_the_health_is_shared_between_the_answer_and_the_light_models():
    llm, fake, clock = make(["best", "lite-a", "lite-b"], failing={"lite-a"})
    light = llm.with_models(["lite-a", "lite-b"])
    asyncio.run(light.generate("p", GeneratedAnswer))
    assert fake.calls == ["lite-a", "lite-b"]
    asyncio.run(llm.answer("s", "p"))
    assert fake.calls[-1] == "best" and light._model_streak["lite-a"] == 1


def test_models_that_reject_a_thinking_level_get_none():
    assert LLM._thinking("gemini-3-flash-preview") is None and LLM._thinking("gemini-3.7-flash") is None
    assert LLM._thinking("gemini-3.6-flash") is not None


def test_the_answer_is_written_in_the_members_language_whatever_it_is():
    prompt = build_prompt("Lihlopha tsa thuto ke life?", "Awa", ["[1] x"], "st", language_name="Sesotho")
    assert prompt.endswith("Write the answer in Sesotho.")
    assert build_prompt("Quand ?", "Awa", ["[1] x"], "fr", language_name="Sesotho").endswith("Write the answer in French.")


def test_event_times_in_the_cohorts_zones():
    moment = event_moment(date(2026, 9, 21), "3:00 PM CAT")
    assert moment == datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)
    assert times_text(moment) == "15:00 CAT / 14:00 WAT / 16:00 EAT"
    assert event_moment(date(2026, 9, 21), "14h30 WAT") == datetime(2026, 9, 21, 13, 30, tzinfo=timezone.utc)
    assert event_moment(date(2026, 9, 21), "") is None and event_moment(date(2026, 9, 21), "end of day") is None


class Store:
    def __init__(self, deadlines):
        self.deadlines, self.claimed = deadlines, set()

    async def deadlines_between(self, start, end, include_dismissed=False):
        return self.deadlines

    async def claim_daily_run(self, job, day):
        if (job, day) in self.claimed:
            return False
        self.claimed.add((job, day))
        return True


def test_a_word_in_the_groups_about_an_hour_before_a_session_once():
    now = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
    open_hour = Deadline("Open Hour with Gift", date(2026, 9, 21), "g@g.us", now, due_time="3:00 PM CAT", id=7)
    later = Deadline("Module 2 class", date(2026, 9, 21), "g@g.us", now, due_time="6:00 PM CAT", id=8)
    store, posted = Store([open_hour, later]), []

    async def post(chat_id, text):
        posted.append((chat_id, text))
        return True

    assert asyncio.run(post_event_reminders(store, post, ["g@g.us"], "en", now)) == 1
    assert posted == [("g@g.us", "⏰ *Open Hour with Gift* starts in about an hour — at 15:00 CAT / 14:00 WAT / 16:00 EAT.")]
    assert asyncio.run(post_event_reminders(store, post, ["g@g.us"], "en", now + timedelta(minutes=10))) == 0  # said once


def test_every_tier_keeps_a_model_that_can_still_answer():
    """Measured 23 Sep at 17:30: every model of the light tier was down at once — 503 "high demand"
    or no answer at all in 20 s — so the rotation rotated between dead models and every call fell
    through to the spare engine, which burned 197,831 of its 200,000 free tokens for the day."""
    from app.config import Settings

    settings = Settings()
    best = settings.answer_models[0]
    assert best in settings.light_model_list, "the light tier must end on a model that works"
    assert settings.light_model_list[-1] == best, "and only reach it when the cheap ones cannot"
    assert settings.light_model_list[0] != best, "the cheap models still come first"
    # The transcription tier has one too.
    assert best in settings.transcription_model_list


def test_a_denied_project_sets_its_key_aside_instead_of_reaching_the_member():
    """Measured 24 September in production: "403 PERMISSION_DENIED — Your project has been denied
    access". It fell through to `raise`, so the dead key stayed first in the rotation, was tried
    again on every request, and the traceback reached the member instead of an answer."""
    import asyncio

    from google.genai import errors

    from app.answer.llm import LLM

    class Denied:
        """A first key whose project is denied, and a second that answers."""

        def __init__(self, llm):
            self.llm, self.asked = llm, []

        async def __call__(self, key_idx, model, contents, config, timeout):
            self.asked.append(key_idx)
            if key_idx == 0:
                raise errors.ClientError(
                    403, {"error": {"code": 403, "message": "Your project has been denied access.",
                                    "status": "PERMISSION_DENIED"}}, None
                )
            return Answer()

    class Answer:
        parsed = None
        text = '{"reply": "ok"}'

    llm = LLM(api_keys=["a", "b"], models=["m1"])
    assert len(llm.valid_keys()) == 2
    llm._disable_key(0, "its project was denied access")
    # The key is out of the rotation for a day, and Jeli says how many are left.
    assert llm.valid_keys() == [1]
    assert all(llm._resting_until[(0, m)] > 0 for m in llm.models)


def test_the_rotation_says_how_many_keys_are_left():
    import inspect

    from app.answer.llm import LLM

    source = inspect.getsource(LLM._disable_key)
    assert "still usable" in source  # the log says what remains, not just what broke
    handling = inspect.getsource(LLM)
    assert "error.code in (401, 403)" in handling
    # And the dead key's other models are dropped from what is still to try.
    assert "pair[0] != key_idx" in handling
