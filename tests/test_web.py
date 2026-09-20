import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.answer.language import TEXTS
from app.config import get_settings
from app.control.activities import Activity, every
from app.main import app
from app.models import Deadline
from app.web.auth import check_password, hash_password
from app.web.chart import nice_ceiling, questions_chart

NOW = datetime.now(timezone.utc)


class FakeStore:
    """What the dashboard reads and writes, in memory."""

    def __init__(self):
        self.audit, self.settings, self.deadlines, self.messages, self.forgiven = [], {}, [], [], []
        self.tries_rows, self.events = [], []
        self.incidents = [{"member_key": "2348185546555", "member_name": "Spammer", "kinds": {"flood": 4, "repeat": 2}, "last_at": NOW}]

    async def usage_since(self, since):
        return {
            "by_kind": {"question": 3, "catchup": 1},
            "questions_by_day": [(NOW.date(), "answered", 2), (NOW.date(), "dont_know", 1)],
            "median_ms": 2400.0,
            "p95_ms": 5000.0,
            "group_questions": [(NOW, "dont_know", "Who judges the <b>bots</b>?"), (NOW, "answered", "When is the deadline?")],
        }

    async def questions_since(self, since, limit=500):
        return [(NOW, "dont_know", "=cmd|' /C calc'!A0"), (NOW, "answered", "When is the deadline?")]

    async def deadlines_between(self, start, end, include_dismissed=False):
        return [d for d in self.deadlines if start <= d.due_date <= end]

    async def add_deadlines(self, deadlines):
        added = [d for d in deadlines if all((d.due_date, d.what.lower()) != (o.due_date, o.what.lower()) for o in self.deadlines)]
        self.deadlines += [Deadline(**{**d.__dict__, "id": len(self.deadlines) + 1}) for d in added]
        return len(added)

    async def dismiss_deadline(self, deadline_id, actor):
        for d in self.deadlines:
            if d.id == deadline_id:
                self.deadlines.remove(d)
                return d.what
        return None

    async def incidents_since(self, since):
        return self.incidents

    async def forgive(self, key):
        self.forgiven.append(key)

    async def knowledge_overview(self):
        return {
            "chats": [{"chat_id": "meti-cohort-2026", "messages": 1125, "last_message": NOW, "live": 0}],
            "recordings": [{"title": "Module 1 <class>", "recorded_at": NOW, "duration_seconds": 6000, "segments": 543, "recaps": ["en", "fr"]}],
            "chunks": 468,
            "pending": 0,
            "deadlines": 1,
        }

    async def live_groups(self, since):
        return []

    async def add_messages(self, messages):
        new = [m for m in messages if m.id not in {k.id for k in self.messages}]
        self.messages += new
        return len(new)

    async def add_audit(self, actor, action):
        self.audit.append((actor, action))

    async def audit_log(self, limit=100):
        return [{"at": NOW, "actor": actor, "action": action} for actor, action in reversed(self.audit)][:limit]

    async def save_settings(self, values, actor):
        self.settings.update(values)

    async def load_settings(self):
        return dict(self.settings)

    async def password_hashes(self):
        return {}

    async def set_password_hash(self, name, password_hash):
        pass

    async def record_incident(self, *args):
        pass

    async def recent_events(self, limit=15):
        return [
            {"id": 2, "at": NOW, "kind": "question", "outcome": "answered", "language": "en", "is_private": False,
             "latency_ms": 2100, "question": "When is the deadline?", "channel": "whatsapp", "chat_id": "meti-cohort-2026"},
            {"id": 1, "at": NOW, "kind": "question", "outcome": "dont_know", "language": "en", "is_private": True,
             "latency_ms": 1800, "question": None, "channel": "whatsapp", "chat_id": ""},
        ]

    async def activity_today(self, since):
        return {"read": 42, "exchanges": 7, "last_message": NOW}

    async def change_marker(self):
        return f"{len(self.audit)}-{len(self.tries_rows)}-{len(self.deadlines)}"

    async def add_try(self, member, role, text, details=None):
        self.tries_rows.append({"member": member, "at": NOW, "role": role, "text": text, "details": details or {}})

    async def tries(self, member, limit=60):
        return [row for row in self.tries_rows if row["member"] == member][-limit:]

    async def clear_tries(self, member):
        self.tries_rows = [row for row in self.tries_rows if row["member"] != member]

    async def record_event(self, event):
        self.events.append(event)


@pytest.fixture
def accounts(monkeypatch):
    monkeypatch.setenv("DASHBOARD_USERS", f"stanley:{hash_password('s3cret-pass')},ede:{hash_password('other-pass')}")
    monkeypatch.setenv("DASHBOARD_SECRET", "test-secret")
    get_settings.cache_clear()


@pytest.fixture
def client(accounts):
    with TestClient(app) as client:
        store = FakeStore()
        app.state.store = store
        app.state.runtime.store = store
        app.state.auth.store = store
        app.state.responder.record = store.record_event
        client.fake_store = store
        yield client


def sign_in(client, name="stanley", password="s3cret-pass"):
    return client.post("/login", data={"name": name, "password": password, "next": "/dashboard"}, follow_redirects=False)


def csrf_of(page_html: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', page_html).group(1)


def test_no_dashboard_without_accounts():
    with TestClient(app) as client:
        assert client.get("/dashboard", follow_redirects=False).status_code == 404
        assert client.get("/login").status_code == 404
        assert client.get("/", follow_redirects=False).headers["location"] == "/dashboard"
        assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_members_sign_in_with_their_own_password(client):
    page = client.get("/dashboard", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login?next=")
    assert sign_in(client, password="other-pass").status_code == 401  # someone else's password
    assert sign_in(client, name="admin").status_code == 401
    signed = sign_in(client)
    assert signed.status_code == 303 and signed.headers["location"] == "/dashboard"
    cookie = signed.headers["set-cookie"]
    assert "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    home = client.get("/dashboard")
    assert home.status_code == 200 and "Jeli is on duty" in home.text and ">Stanley<" in home.text
    assert ("stanley", "Signed in") in client.fake_store.audit


def test_password_guessing_is_slowed_down(client):
    for _ in range(5):
        assert sign_in(client, password="guess").status_code == 401
    assert sign_in(client).status_code == 429  # even the right password waits now


def test_a_forged_session_or_change_is_refused(client):
    client.cookies.set("jeli_session", "stanley|99999999999|abcd|forged")
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
    client.cookies.clear()
    sign_in(client)
    assert client.post("/dashboard/pause", data={"action": "pause"}).status_code == 403  # no token
    token = csrf_of(client.get("/dashboard").text)
    assert client.post("/dashboard/pause", data={"action": "pause", "csrf": token}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_every_page_opens_and_shows_nothing_technical(client):
    sign_in(client)
    for path in ("/dashboard", "/dashboard/try", "/dashboard/questions", "/dashboard/deadlines", "/dashboard/knowledge",
                 "/dashboard/activities", "/dashboard/watchlist", "/dashboard/exceptions", "/dashboard/settings",
                 "/dashboard/whatsapp", "/dashboard/team"):
        page = client.get(path)
        assert page.status_code == 200, path
        shown = " ".join(re.findall(r"\w[\w@.]*", re.sub(r"<[^>]+>", " ", page.text.split("<script>")[0]).lower()))
        for jargon in ("gemini", "chunk", "similarity", "embedding", "@g.us", "utc", "2348185546555"):
            assert jargon not in shown.split(), (path, jargon)
    questions = client.get("/dashboard/questions").text
    assert "=cmd|" in questions  # shown, escaped
    home = client.get("/dashboard").text
    assert "Who judges" not in home or "&lt;b&gt;" in home


def test_pausing_jeli_is_one_click_and_logged(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard").text)
    paused = client.post("/dashboard/pause", data={"action": "pause", "csrf": token, "next": "/dashboard/settings"}, follow_redirects=False)
    assert paused.headers["location"] == "/dashboard/settings"
    assert app.state.runtime.paused and ("stanley", "Paused Jeli") in client.fake_store.audit
    page = client.get("/dashboard/settings").text
    assert "Jeli is paused: it answers nobody" in page and "Resume Jeli" in page
    client.post("/dashboard/pause", data={"action": "resume", "csrf": token})
    assert not app.state.runtime.paused


def test_try_jeli_answers_as_on_whatsapp_and_keeps_each_members_conversation(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/try").text)
    headers = {"X-CSRF-Token": token}
    reply = client.post("/dashboard/try", json={"text": "When is the deadline?", "mode": "ask", "chat": "meti-cohort-2026"}, headers=headers)
    assert reply.status_code == 200
    jeli = reply.json()["jeli"]
    assert jeli["text"] == TEXTS["en"]["not_ready"] and jeli["quoted"] == ["You", "When is the deadline?"]  # replies to the question
    greeting = client.post("/dashboard/try", json={"text": "Who are you?", "mode": "ask", "chat": "private"}, headers=headers).json()
    assert greeting["jeli"]["text"] == TEXTS["en"]["about_jeli"]
    assert client.post("/dashboard/try", json={"text": "hi"}).status_code == 403  # no token
    # Kept for this member only, and shown again on the page.
    assert [row["role"] for row in client.fake_store.tries_rows] == ["member", "jeli", "member", "jeli"]
    page = client.get("/dashboard/try").text
    assert "When is the deadline?" in page and "Writing in" in page
    # Every try is an exchange the team sees live.
    assert {event.channel for event in client.fake_store.events} == {"dashboard"}
    client.post("/dashboard/try/clear", data={"csrf": token})
    assert client.fake_store.tries_rows == []


def test_try_jeli_plays_the_voice_note_jeli_would_send(client):
    class Voice:
        async def speak(self, text):
            self.said = text
            return b"RIFF-voice"

    app.state.voice = voice = Voice()
    sign_in(client)
    headers = {"X-CSRF-Token": csrf_of(client.get("/dashboard/try").text)}
    jeli = client.post("/dashboard/try", json={"text": "Who are you? Reply by voice", "mode": "ask", "chat": "private"}, headers=headers).json()["jeli"]
    assert jeli["text"] == TEXTS["en"]["about_jeli"]  # the question, without "reply by voice"
    assert jeli["voice"] == "data:audio/wav;base64,UklGRi12b2ljZQ==" and voice.said.startswith("Hey, I'm Jeli")
    assert "/catchup" not in voice.said and "METI UniPods AI Programme" in voice.said  # commands are not read out
    kept = client.fake_store.tries_rows[-1]
    assert "voice" not in kept["details"] and kept["details"]["spoken"] is True  # the audio itself is not kept
    app.state.voice = None


def test_live_pages_answer_304_until_something_changes(client):
    sign_in(client)
    first = client.get("/dashboard", headers={"X-Live": "1"})
    etag = first.headers["etag"]
    assert first.status_code == 200 and 'id="live-body"' in first.text and "Live conversations" in first.text
    assert "42</b> messages read" in first.text and "When is the deadline?" in first.text
    assert client.get("/dashboard", headers={"X-Live": "1", "If-None-Match": etag}).status_code == 304
    token = csrf_of(client.get("/dashboard").text)
    client.post("/dashboard/pause", data={"action": "pause", "csrf": token})
    assert client.get("/dashboard", headers={"X-Live": "1", "If-None-Match": etag}).status_code == 200


def test_settings_are_saved_applied_and_validated(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/settings").text)
    form = {
        "csrf": token, "care": "careful", "bot_name": "Jeli", "pointer_care": "balanced", "duplicate_replies_per_hour": "2",
        "whatsapp_user_limit": "4", "whatsapp_hourly_limit": "60", "whatsapp_min_send_interval_seconds": "4",
    }
    client.post("/dashboard/settings", data=form)
    runtime = app.state.runtime
    assert runtime["answer_min_similarity"] == 0.63 and runtime["whatsapp_user_limit"] == 4
    assert runtime["duplicate_detection"] is False  # unticked
    assert app.state.responder.uninvited.limit == 2 and app.state.answerer is None
    assert client.fake_store.settings["whatsapp_user_limit"] == 4
    assert any(action.startswith("Changed ") for _, action in client.fake_store.audit)
    page = client.post("/dashboard/settings", data={**form, "whatsapp_hourly_limit": "5000"}).text
    assert "Check “answers per hour”" in page and runtime["whatsapp_hourly_limit"] == 60


def test_exceptions_and_the_watchlist_block_people(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/exceptions").text)
    client.post("/dashboard/exceptions", data={"csrf": token, "list": "ignored_authors", "action": "add", "value": "+229 01 49 48 62 56"})
    assert app.state.runtime["ignored_authors"] == ["+229 01 49 48 62 56"]
    watch = client.get("/dashboard/watchlist").text
    assert "Spammer" in watch and "Asked too many questions" in watch and "2348185546555" not in watch
    ref = re.search(r'name="ref" value="([0-9a-f]+)"', watch).group(1)
    client.post("/dashboard/watchlist", data={"csrf": token, "ref": ref, "action": "block"})
    assert "2348185546555" in app.state.guard.blocked
    assert "Blocked by the team" in client.get("/dashboard/watchlist").text
    client.post("/dashboard/watchlist", data={"csrf": token, "ref": ref, "action": "unblock"})
    assert not app.state.guard.blocked
    client.post("/dashboard/watchlist", data={"csrf": token, "ref": ref, "action": "forgive"})
    assert client.fake_store.forgiven == ["2348185546555"]


def test_deadlines_are_added_and_removed_by_the_team(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/deadlines").text)
    due = (NOW + timedelta(days=3)).date().isoformat()
    client.post("/dashboard/deadlines/add", data={"csrf": token, "what": "Submit the pitch deck", "due_date": due, "due_time": "", "programme": "hackathon"})
    [deadline] = client.fake_store.deadlines
    assert deadline.chat_id == "team" and deadline.author == "Stanley"
    assert "Submit the pitch deck" in client.get("/dashboard/deadlines").text
    client.post("/dashboard/deadlines/remove", data={"csrf": token, "id": str(deadline.id)})
    assert client.fake_store.deadlines == []
    assert ("stanley", "Removed the deadline “Submit the pitch deck”") in client.fake_store.audit


EXPORT = """17/09/2026, 20:53 - Awa: The build phase runs from 18 to 24 September
17/09/2026, 20:55 - +234 818 554 6555: Thanks!
"""


def test_old_conversations_are_checked_then_added(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/knowledge").text)
    preview = client.post(
        "/dashboard/knowledge/upload",
        data={"csrf": token, "chat": "", "label": "METI before Jeli", "timezone": "UTC"},
        files={"file": ("WhatsApp Chat.txt", EXPORT.encode(), "text/plain")},
        follow_redirects=False,
    )
    assert preview.status_code == 303 and "preview=" in preview.headers["location"]
    page = client.get(preview.headers["location"]).text
    assert "2 messages" in page and "2 people" in page and "METI before Jeli" in page
    assert client.fake_store.messages == []  # nothing added before the member confirms
    preview_token = preview.headers["location"].split("preview=")[1]
    done = client.post("/dashboard/knowledge/import", data={"csrf": token, "token": preview_token}).text
    assert "2 new messages added" in done
    assert {m.chat_id for m in client.fake_store.messages} == {"meti-before-jeli"}
    assert app.state.runtime["chat_labels"]["meti-before-jeli"] == "METI before Jeli"
    again = client.post("/dashboard/knowledge/import", data={"csrf": token, "token": preview_token}).text
    assert "expired" in again  # a preview is used once


def test_activities_can_be_switched_run_and_stopped(client):
    sign_in(client)
    runs = []

    async def work():
        runs.append(1)
        return "did it"

    activity = Activity("memory", "Keeping Jeli's memory up to date", "desc", work, every(timedelta(hours=1)), lambda: app.state.runtime["enabled.memory"])
    app.state.activities = {"memory": activity}
    token = csrf_of(client.get("/dashboard/activities").text)
    client.post("/dashboard/activities", data={"csrf": token, "key": "memory", "action": "off"})
    assert app.state.runtime["enabled.memory"] is False
    assert "Switched off" in client.get("/dashboard").text
    client.post("/dashboard/activities", data={"csrf": token, "key": "memory", "action": "run"})
    assert "did it" in client.get("/dashboard/activities").text and runs == [1]


def test_members_change_their_password(client):
    sign_in(client)
    token = csrf_of(client.get("/dashboard/team").text)
    page = client.post("/dashboard/team/password", data={"csrf": token, "current": "wrong", "new": "a-new-password", "again": "a-new-password"}).text
    assert "current password is not right" in page
    page = client.post("/dashboard/team/password", data={"csrf": token, "current": "s3cret-pass", "new": "short", "again": "short"}).text
    assert "at least 10 characters" in page


def test_passwords_are_kept_as_salted_hashes():
    stored = hash_password("s3cret")
    assert "s3cret" not in stored and stored != hash_password("s3cret")
    assert check_password("s3cret", stored) and not check_password("s3cret ", stored)
    assert not check_password("s3cret", "not-hex:00")


def test_chart_axis_and_columns():
    assert nice_ceiling(0) == (4, 1) and nice_ceiling(7) == (8, 2) and nice_ceiling(23) == (25, 5) and nice_ceiling(130) == (150, 50)
    days = [date(2026, 9, 18), date(2026, 9, 19)]
    svg = questions_chart(days, {(days[0], "answered"): 5, (days[1], "answered"): 5, (days[1], "dont_know"): 2})
    assert svg.count('class="s1"') == 2 and svg.count('class="s2"') == 1 and ">7</text>" in svg
    assert "Sat 19 Sep: 7 questions — 5 answered, 2 couldn&#x27;t answer" in svg
    assert "No questions yet" in questions_chart(days, {})
