import asyncio
from datetime import date, datetime, timezone

from fastapi.testclient import TestClient

from app.answer.language import TEXTS
from app.answer.responder import Responder
from app.config import get_settings
from app.dashboard import _chart, _nice_ceiling, render
from app.main import app
from app.models import IncomingMessage

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def test_dashboard_is_off_without_a_password_and_protected_with_one(monkeypatch):
    with TestClient(app) as client:
        assert client.get("/dashboard").status_code == 404
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    get_settings.cache_clear()
    with TestClient(app) as client:
        missing = client.get("/dashboard")
        assert missing.status_code == 401 and missing.headers["www-authenticate"].startswith("Basic")
        assert client.get("/dashboard", auth=("admin", "wrong")).status_code == 401
        page = client.get("/dashboard", auth=("admin", "s3cret"))
    assert page.status_code == 200
    assert "Jeli dashboard" in page.text and "No knowledge base configured" in page.text
    assert page.headers["cache-control"] == "no-store"


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

    message = IncomingMessage("whatsapp", "g@g.us", "1", "Awa", "What is the capital of Japan?", NOW, False, True)
    asyncio.run(Responder(Answerer(), record=record).respond(message))
    [event] = recorded
    assert (event.kind, event.outcome, event.language, event.is_private) == ("question", "dont_know", "en", False)
    assert event.latency_ms is not None
    assert not hasattr(event, "text") and not hasattr(event, "author")
