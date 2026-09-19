import asyncio
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.answer.language import TEXTS
from app.answer.responder import Responder, shareable_question
from app.config import get_settings
from app.dashboard import _chart, _nice_ceiling, check_password, hash_password, render
from app.main import app
from app.models import IncomingMessage

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_dashboard_is_off_without_accounts_and_each_member_has_a_password(monkeypatch):
    with TestClient(app) as client:
        assert client.get("/dashboard").status_code == 404
        assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
    monkeypatch.setenv("DASHBOARD_USERS", f"stanley:{hash_password('s3cret')}, Ede:{hash_password('other')}")
    get_settings.cache_clear()
    with TestClient(app) as client:
        missing = client.get("/dashboard")
        assert missing.status_code == 401 and missing.headers["www-authenticate"].startswith("Basic")
        assert client.get("/dashboard", auth=("stanley", "other")).status_code == 401  # someone else's password
        assert client.get("/dashboard", auth=("admin", "s3cret")).status_code == 401
        page = client.get("/dashboard", auth=("Stanley", "s3cret"))
        assert client.get("/dashboard", auth=("ede", "other")).status_code == 200
    assert page.status_code == 200
    assert "Jeli dashboard" in page.text and "No knowledge base configured" in page.text and "Signed in as stanley" in page.text
    assert page.headers["cache-control"] == "no-store"


def test_passwords_are_kept_as_salted_hashes():
    stored = hash_password("s3cret")
    assert "s3cret" not in stored and stored != hash_password("s3cret")
    assert check_password("s3cret", stored) and not check_password("s3cret ", stored)
    assert not check_password("s3cret", "not-hex:00")


def test_no_public_api_documentation():
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_axis_maximum_is_round():
    assert _nice_ceiling(0) == (4, 1)
    assert _nice_ceiling(7) == (8, 2)
    assert _nice_ceiling(23) == (25, 5)
    assert _nice_ceiling(130) == (150, 50)


USAGE = {
    "by_kind": {"question": 12, "catchup": 3, "recap": 2, "already_answered": 1},
    "questions_by_day": [(date(2026, 9, 18), "answered", 5), (date(2026, 9, 19), "answered", 5), (date(2026, 9, 19), "dont_know", 2)],
    "median_ms": 2800.0,
    "p95_ms": 6400.0,
    "group_questions": [
        (NOW, "dont_know", "Who judges the <b>bots</b>?"),
        (NOW, "answered", "When is the submission deadline?"),
    ],
}
KNOWLEDGE = {
    "chats": [{"chat_id": "meti", "messages": 1125, "live": 0, "last_message": NOW}],
    "recordings": [{"title": "Module 1 <class>", "recorded_at": NOW, "duration_seconds": 6000, "segments": 543, "recaps": ["en", "fr"]}],
    "chunks": 468,
    "pending": 0,
    "deadlines": 4,
}


def test_page_shows_counts_but_escapes_every_label():
    page = render(NOW, [("WhatsApp", "warning", "number not linked yet (FAILED)")], USAGE, KNOWLEDGE, {"meti": "METI cohort"})
    assert "<b>WhatsApp</b> number not linked yet (FAILED)" in page
    assert ">12<" in page and ">83%<" in page and ">2.8 s<" in page and "95% under 6.4 s" in page
    assert "METI cohort" in page and "1,125" in page and "EN, FR" in page
    assert "Module 1 &lt;class&gt;" in page and "<class>" not in page
    assert "Show as a table" in page  # the chart always has a table view
    assert "Jeli could not answer (1)" in page and "All questions (2)" in page
    assert "Who judges the &lt;b&gt;bots&lt;/b&gt;?" in page and "When is the submission deadline?" in page


def test_chart_stacks_outcomes_with_totals_and_tooltips():
    days = [date(2026, 9, 18), date(2026, 9, 19)]
    svg = _chart(days, {(days[0], "answered"): 5, (days[1], "answered"): 5, (days[1], "dont_know"): 2})
    assert svg.count('class="s1"') == 2 and svg.count('class="s2"') == 1
    assert "<title>Sat 19 Sep: 7 questions — 5 answered, 2 “i don&#x27;t know”, 0 sources only (models down)</title>" in svg
    assert ">7</text>" in svg  # total on the cap
    assert "No questions yet" in _chart(days, {})


def test_replies_are_counted_without_text_or_author():
    recorded = []

    async def record(event):
        recorded.append(event)

    class Answerer:
        async def answer(self, question, asker):
            return TEXTS["en"]["dont_know"]

    responder = Responder(Answerer(), record=record)
    asked = IncomingMessage("whatsapp", "g@g.us", "1", "Awa", "@22901020304 What is the capital of Japan?", NOW, False, True)
    in_private = IncomingMessage("whatsapp", "awa@c.us", "2", "Awa", "What is my score?", NOW, True, True)
    asyncio.run(responder.respond(asked))
    asyncio.run(responder.respond(in_private))
    group, private = recorded
    assert (group.kind, group.outcome, group.language, group.is_private) == ("question", "dont_know", "en", False)
    assert group.latency_ms is not None and group.question == "What is the capital of Japan?"
    assert private.is_private and private.question == ""  # private questions are only counted
    assert not hasattr(group, "author")


def test_shared_questions_carry_no_phone_number():
    assert shareable_question("Can +234 818 554 6555 join?") == "Can ··· join?"
    assert shareable_question("Is it due 2026-09-24 at 9:00?") == "Is it due 2026-09-24 at 9:00?"
    assert len(shareable_question("why " * 200)) == 300
