"""Jeli's public page: what a member of the community sees, and what it caps."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app
from app.web.public import MESSAGES_ALLOWED, VOICE_ALLOWED, Visits


class Store:
    def __init__(self, known=None):
        self.known = dict(known or {"22370000000": "Awa Traoré"})

    async def member_by_ids(self, ids):
        return next((self.known[i] for i in ids if i in self.known), None)

    async def knowledge_overview(self):
        return {"chats": [{"messages": 911}], "recordings": [1, 2], "totals": {"deadlines": 7}}

    async def load_settings(self):
        return {}

    async def record_event(self, *a, **kw):
        return None


@pytest.fixture
def public(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SECRET", "a-test-secret")
    monkeypatch.setenv("DASHBOARD_USERS", "stanley:c2FsdA==:x")
    get_settings.cache_clear()
    with TestClient(app) as client:
        app.state.store = Store()
        app.state.visits = Visits()

        async def respond(message):
            return f"Réponse à : {message.text}"

        app.state.responder = type("R", (), {"respond": staticmethod(respond)})()
        app.state.voice = None
        yield client


def enter(client, number="+223 70 00 00 00"):
    return client.post("/jeli/enter", data={"number": number}, follow_redirects=False)


def test_a_member_enters_with_the_number_the_group_knows(public):
    assert "numéro WhatsApp" in public.get("/jeli").text  # not signed in yet
    assert enter(public).status_code == 303
    page = public.get("/jeli").text
    assert "Awa" in page and "911" in page.replace("&nbsp;", " ").replace(" ", " ")
    assert "sait faire" in page and "Messages gard" in page


def test_a_number_jeli_has_never_read_is_refused(public):
    page = enter(public, "+1 555 000 111")
    assert page.status_code == 200 and "ne reconnaît pas ce numéro" in page.text
    assert public.get("/jeli/ask").status_code in (401, 405)


def test_the_page_can_change_nothing(public):
    """It is read-only by construction: there is no route here that writes a setting."""
    from app.web.public import router

    writes = [r for r in router.routes if "POST" in getattr(r, "methods", set())]
    assert {r.path for r in writes} == {"/jeli/enter", "/jeli/ask"}


def test_a_visitor_gets_ten_messages_then_the_real_conversation(public):
    enter(public)
    for n in range(MESSAGES_ALLOWED):
        answer = public.post("/jeli/ask", json={"text": f"question {n}"}).json()
        assert answer["reply"].startswith("Réponse à")
        assert answer["left"] == MESSAGES_ALLOWED - n - 1
    finished = public.post("/jeli/ask", json={"text": "encore une"}).json()
    assert finished["finished"] and "WhatsApp" in finished["reply"]
    assert "Réponse à" not in finished["reply"]  # it really stops answering


def test_the_team_is_never_capped(monkeypatch, public):
    monkeypatch.setenv("TEAM_NUMBERS", "22370000000")
    get_settings.cache_clear()
    enter(public)
    for _ in range(MESSAGES_ALLOWED + 3):
        answer = public.post("/jeli/ask", json={"text": "test"}).json()
        assert answer["reply"].startswith("Réponse à")


def test_the_visitor_cookie_cannot_be_forged(public):
    enter(public)
    cookie = public.cookies.get("jeli_visitor")
    public.cookies.set("jeli_visitor", cookie[:-1] + ("0" if cookie[-1] != "0" else "1"))
    assert public.post("/jeli/ask", json={"text": "hello"}).status_code == 401


def test_voice_is_capped_more_tightly_than_writing():
    visits = Visits()
    for _ in range(VOICE_ALLOWED):
        visits.used("22399999999", spoken_reply=True)
    left, voice_left = visits.left("22399999999")
    assert voice_left == 0 and left == MESSAGES_ALLOWED - VOICE_ALLOWED


def test_a_member_who_never_wrote_can_still_come_in(monkeypatch):
    """Measured 23 Sep: of a 240-person cohort, 79 had ever written. Asking Jeli whether it had
    read someone turned its own page away from two members out of three."""
    import asyncio

    from app.adapters.whatsapp_waha import Waha
    from app.config import Settings

    people = [
        {"id": "111111111111111@lid", "name": "Awa Traoré"},
        {"lid": "222222222222222@lid", "pn": "22389987255@c.us", "name": "Silent Member"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/participants"):
            return httpx.Response(200, json=people)
        return httpx.Response(200, json={})  # WhatsApp knows no id for that number

    async def respond(message):
        return None

    waha = Waha(Settings(waha_url="http://waha.test", waha_api_key="k"), respond)
    waha._http = httpx.AsyncClient(base_url="http://waha.test", transport=httpx.MockTransport(handler))
    found = asyncio.run(waha.group_member("+223 89 98 72 55", ["120363000000000000@g.us"]))
    assert found == "Silent Member"
    # Someone who is in no group is still turned away.
    assert asyncio.run(waha.group_member("+1 555 000 111", ["120363000000000000@g.us"])) is None
