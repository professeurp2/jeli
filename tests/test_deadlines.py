import asyncio
from datetime import date, datetime, timedelta, timezone

from app.answer.deadlines import DeadlineExtractor, Deadlines, FoundDeadline, FoundDeadlines, _validated, same_deadline
from app.answer.intents import is_deadlines_request
from app.answer.llm import LLMUnavailable
from app.models import Deadline, StoredMessage

THU_17 = datetime(2026, 9, 17, 20, 53, tzinfo=timezone.utc)


def message(n, text, author="Awa", when=THU_17, chat="meti"):
    return StoredMessage(f"m{n}", chat, "whatsapp_export", author, when, text)


def found(*items):
    return FoundDeadlines(deadlines=[FoundDeadline(what=w, due_date=d, due_time=t, programme=p, message=m) for w, d, t, p, m in items])


def test_only_plausible_deadlines_tied_to_a_real_message_are_kept():
    messages = [message(1, "Submit by Thursday 24 Sep"), message(2, "GABI video due tomorrow 2 PM CAT")]
    kept = _validated(
        found(
            ("Hackathon: submit the chatbot", "2026-09-24", "", "hackathon", 1),
            ("Submit the GABI video", "2026-09-18", "2:00 PM CAT", "MIT Universal AI", 2),
            ("Not a date", "Friday", "", "", 1),
            ("Wrong message number", "2026-09-24", "", "", 9),
            ("Long before the message", "2026-08-01", "", "", 1),
            ("A year later", "2027-09-24", "", "", 1),
        ),
        messages,
    )
    assert [(d.what, d.due_date, d.message_id) for d in kept] == [
        ("Hackathon: submit the chatbot", date(2026, 9, 24), "m1"),
        ("Submit the GABI video", date(2026, 9, 18), "m2"),
    ]
    assert kept[1].due_time == "2:00 PM CAT" and kept[0].author == "Awa" and kept[0].announced_at == THU_17


class FakeStore:
    def __init__(self, messages):
        self.unchecked = list(messages)
        self.deadlines = []
        self.checked = []

    async def unchecked_messages(self, limit):
        batch, self.unchecked = self.unchecked[:limit], self.unchecked[limit:]
        return batch

    async def mark_deadlines_checked(self, ids):
        self.checked.extend(ids)

    async def add_deadlines(self, deadlines):
        self.deadlines.extend(deadlines)
        return len(deadlines)

    async def deadlines_between(self, start, end, include_dismissed=False):
        return sorted((d for d in self.deadlines if start <= d.due_date <= end), key=lambda d: d.due_date)


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.prompts = result, error, []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


def test_extraction_scans_messages_without_other_bots_and_marks_them():
    store = FakeStore([message(1, "Submit by Thursday 24 Sep"), message(2, "I am a bot", author="OtherBot")])
    llm = FakeLLM(found(("Hackathon: submit the chatbot", "2026-09-24", "", "hackathon", 1)))
    added = asyncio.run(DeadlineExtractor(store, llm, ignored_authors=["OtherBot"], chat_labels={"meti": "METI cohort"}).run())
    assert added == 1
    assert store.checked == ["m1", "m2"]
    [prompt] = llm.prompts
    assert "[1] Thu 17 Sep 2026 20:53 UTC · METI cohort · Awa: Submit by Thursday 24 Sep" in prompt
    assert "I am a bot" not in prompt


def test_a_run_can_stop_after_a_few_batches():
    store = FakeStore([message(n, f"Message {n}") for n in range(150)])
    asyncio.run(DeadlineExtractor(store, FakeLLM(found())).run(max_batches=2))
    assert len(store.checked) == 120 and len(store.unchecked) == 30


def test_known_deadlines_are_given_to_the_model_to_avoid_repeats():
    store = FakeStore([message(1, "Reminder: submissions close Thursday")])
    store.deadlines.append(Deadline("Hackathon: submit the chatbot", date(2026, 9, 24), "meti", THU_17))
    llm = FakeLLM(found())
    asyncio.run(DeadlineExtractor(store, llm).run())
    assert "- 2026-09-24 Hackathon: submit the chatbot" in llm.prompts[0]


def test_a_model_failure_leaves_messages_to_scan_again():
    store = FakeStore([message(1, "Submit by Thursday 24 Sep")])
    try:
        asyncio.run(DeadlineExtractor(store, FakeLLM(error=LLMUnavailable())).run())
    except LLMUnavailable:
        pass
    assert store.checked == []


def test_upcoming_deadlines_reply_and_digest_section():
    store = FakeStore([])
    store.deadlines = [
        Deadline("Hackathon: submit the chatbot", date(2026, 9, 24), "meti", THU_17, due_time="9:00 CAT", author="+234 818 554 6555"),
        Deadline("Complete Module 1 activities", date(2026, 9, 22), "recording:module1", THU_17, author="Charles Bolton"),
        Deadline("Far away", date(2026, 11, 1), "meti", THU_17),
    ]
    deadlines = Deadlines(store, chat_labels={"meti": "METI cohort"})
    today = date(2026, 9, 19)
    reply = asyncio.run(deadlines.upcoming_reply("en", today=today))
    assert reply.splitlines() == [
        "⏰ Deadlines in the next 14 days",
        "• Tue 22 Sep — Complete Module 1 activities (call, Charles Bolton, Thu 17 Sep)",
        "• Thu 24 Sep, 9:00 CAT — Hackathon: submit the chatbot (METI cohort, +234 ···55, Thu 17 Sep)",
    ]
    section = asyncio.run(deadlines.coming_up_section("fr", days=3, today=today))
    assert section.splitlines() == ["⏰ À venir", "• mar. 22 sept. — Complete Module 1 activities (appel, Charles Bolton, jeu. 17 sept.)"]
    assert asyncio.run(deadlines.coming_up_section("en", days=1, today=today)) is None
    assert asyncio.run(Deadlines(FakeStore([])).upcoming_reply("fr", today=today)) == "Aucune échéance annoncée pour les 14 prochains jours."


def test_two_wordings_of_one_deadline_are_one_deadline():
    def d(what, day=22):
        return Deadline(what, date(2026, 9, day), "recording:m1", THU_17)

    assert same_deadline(d("Wadhwani Ignite: complete Module 1"), d("Wadhwani Ignite: complete module one lesson work"))
    assert not same_deadline(d("Submit Module 1 activities"), d("Submit Module 2 activities"))
    assert not same_deadline(d("Complete Module 1"), d("Complete Module 1", day=24))
    assert not same_deadline(d("Talk about the customers"), d("Complete Module 1"))


def test_deadline_requests():
    assert is_deadlines_request("/deadlines")
    assert is_deadlines_request("What are the upcoming deadlines?")
    assert is_deadlines_request("Quelles sont les prochaines échéances ?")
    assert not is_deadlines_request("When is the deadline for the hackathon?")  # a question: grounded answer
