"""Every way Jeli's engines can fail, replayed against the real code, with every decision printed.

    python -m scripts.audit_switching

Run it yourself. It calls the same LLM and Groq classes production uses, with the real settings;
only the network is replaced, so what you read is what Jeli would do. It proves the switching, not
Google's behaviour: what a model does on the day is measured against the key, not simulated here.

The scenarios are the failures that really happened, with their dates. The last three are the
evening of 23 September, when every light model was down at once and the spare engine had spent
its day — the case the first audit could not see, because it gave the spare engine a network that
always answered.
"""

import asyncio
import json
import logging
import sys
from types import SimpleNamespace

import httpx
from google.genai import errors

sys.path.insert(0, ".")
logging.disable(logging.WARNING)

from app.answer.groq import Groq  # noqa: E402
from app.answer.llm import LLM, GeneratedAnswer, LLMUnavailable  # noqa: E402
from app.config import get_settings  # noqa: E402

GOOD = GeneratedAnswer(answered=True, answer="ok", sources=["1"])
ANSWER_MODELS = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite"]


def gemini_error(kind: str):
    if kind == "quota day":
        return errors.ClientError(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED: GenerateRequestsPerDayPerProjectPerModel"}})
    if kind == "quota minute":
        return errors.ClientError(429, {"error": {"code": 429, "message": "RESOURCE_EXHAUSTED: PerMinute"}})
    if kind == "bad key":
        return errors.ClientError(401, {"error": {"code": 401, "message": "API key not valid"}})
    if kind == "overloaded":
        return errors.ServerError(503, {"error": {"code": 503, "message": "This model is currently experiencing high demand"}})
    return TimeoutError()


def groq_network(rule, log):
    """A Groq that answers as `rule(model, prompt_chars)` says: "ok", "minute", "day" or "down"."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        body = json.loads(request.read())
        model, said = body["model"], body["messages"][1]["content"]
        what = rule(model, len(said))
        log.append(f"      spare {model} ({len(said):,} chars) -> {what}")
        if what == "ok":
            return httpx.Response(200, json={"choices": [{"message": {"content": GOOD.model_dump_json()}}]})
        if what == "minute":
            return httpx.Response(429, json={"error": {"message": (
                "Rate limit reached for model `x` on input tokens per minute (ITPM): "
                "Limit 7000, Used 5329, Requested 5548. Please try again in 33s.")}})
        if what == "day":
            return httpx.Response(429, json={"error": {"message": (
                "Rate limit reached for model `x` on tokens per day (TPD): "
                "Limit 200000, Used 197831, Requested 3694. Please try again in 10m58s.")}})
        return httpx.Response(503, json={"error": {"message": "service unavailable"}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def build(models, rule, keys=4, spare=None, engine="auto"):
    log: list[str] = []
    state = SimpleNamespace(key=0)

    class PerKey:
        def __init__(self, index):
            self.index = index
            self.aio = SimpleNamespace(models=self)

        async def generate_content(self, model, contents, config):
            what = rule(self.index, model)
            log.append(f"      gemini key {self.index} {model} -> {what}")
            if what == "ok":
                return SimpleNamespace(parsed=GOOD, text=GOOD.model_dump_json())
            raise gemini_error(what)

    backup = None
    if spare is not None:
        backup = Groq("k", ["gpt-oss-120b", "qwen3"], clock=lambda: 0.0, client=groq_network(spare, log))
    llm = LLM("unused", list(models), client=PerKey(0), clock=lambda: 0.0, backup=backup)
    llm._clients = [PerKey(i) for i in range(keys)]
    llm.engine = engine
    return llm, log


async def run(title, note, models, rule, spare=None, engine="auto", chars=200, calls=1, keys=4):
    llm, log = build(models, rule, keys=keys, spare=spare, engine=engine)
    print(f"\n{title}\n   {note}")
    for call in range(calls):
        if calls > 1:
            log.append(f"    question {call + 1}")
        before = getattr(llm.backup, "used", 0)
        try:
            await llm.generate("x" * chars, GeneratedAnswer, timeout=6)
            # Which engine answered THIS question, not whether the spare one ever has.
            spare_answered = getattr(llm.backup, "used", 0) > before
            log.append(f"    -> ANSWERED by {'the spare engine' if spare_answered else 'Gemini'}")
        except LLMUnavailable:
            log.append("    -> NO ANSWER (the member is told Jeli cannot right now)")
    print("\n".join(log))


async def main():
    settings = get_settings()
    print("Jeli's engines, as configured right now")
    print("   answers:", ", ".join(settings.answer_models))
    print("   light  :", ", ".join(settings.light_model_list))
    print("   spare  : Groq" + ("" if settings.groq_api_key else " (no key set here)"))

    await run(
        "1. The best model is out of quota for the day (21 Sep)",
        "A daily quota belongs to the key: the other keys are tried, then the next model.",
        ANSWER_MODELS, lambda k, m: "quota day" if m == ANSWER_MODELS[0] else "ok",
    )
    await run(
        "2. Two models overloaded (22 Sep, 16:48)",
        "An overloaded model is dropped for every key at once: it is Google's state, not the key's.",
        ANSWER_MODELS, lambda k, m: "overloaded" if m in ANSWER_MODELS[:2] else "ok",
    )
    await run(
        "3. A key was revoked",
        "Key 0 is dropped for 24 h; the next question does not pay for it again.",
        ANSWER_MODELS, lambda k, m: "bad key" if k == 0 else "ok", calls=2,
    )
    await run(
        "4. Every Gemini model down, no spare engine",
        "Jeli says it cannot, quickly, instead of making the member wait.",
        ANSWER_MODELS, lambda k, m: "overloaded",
    )
    await run(
        "5. Every Gemini model down, spare engine healthy (22 Sep, 16:48)",
        "Two slow failures are enough to conclude an outage; the spare engine answers.",
        ANSWER_MODELS, lambda k, m: "overloaded", spare=lambda m, n: "ok",
    )
    await run(
        "6. THE EVENING OF 23 SEP: every light model down, spare engine over its minute budget",
        "A per-minute token limit is a size: the source is halved until it fits. This is the case "
        "the first audit could not see, and the one that showed the member a failure.",
        settings.light_model_list,
        lambda k, m: "ok" if m == settings.answer_models[0] else ("overloaded" if "3.1" in m else "timeout"),
        spare=lambda m, n: "ok" if n <= 20_000 else "minute", chars=80_000, calls=4,
    )
    await run(
        "7. The spare engine has spent its day (23 Sep, 17:16)",
        "Nothing fits once the day's budget is gone: that model rests an hour, the next one answers.",
        ANSWER_MODELS, lambda k, m: "overloaded",
        spare=lambda m, n: "day" if m.startswith("gpt-oss") else "ok",
    )
    await run(
        "8. Both engines gone at once",
        "The only case where the member is told Jeli cannot: it is true, and it is said plainly.",
        ANSWER_MODELS, lambda k, m: "overloaded", spare=lambda m, n: "down",
    )
    for engine, note in (
        ("gemini_only", "Gemini only: the spare engine is never asked, even when Gemini fails."),
        ("backup_only", "Spare only: Gemini is never asked, and a failure is a failure."),
        ("backup", "Spare first: it answers, and Gemini is the fallback."),
    ):
        await run(
            f"9. The team chose “{engine}” on the dashboard", note,
            ANSWER_MODELS, lambda k, m: "ok", spare=lambda m, n: "ok", engine=engine,
        )


asyncio.run(main())
