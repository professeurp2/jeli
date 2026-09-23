"""The super admin steers Jeli in plain words — and only that one number can."""

import asyncio

import pytest

from app.config import Settings
from app.control.admin import Admin, is_super_admin
from app.control.runtime import Runtime


class Model:
    """A model that turns a sentence into the command given to it."""

    def __init__(self, command):
        self.command = command
        self.asked = []

    async def generate(self, contents, schema, system=None, **kw):
        self.asked.append((contents, system))
        return schema(**self.command)


def runtime() -> Runtime:
    return Runtime(Settings(), store=None)


def test_only_the_super_admins_own_number_commands_jeli():
    assert is_super_admin("+223 93 05 69 36", "22393056936@c.us")
    assert is_super_admin("22393056936", "22393056936:12@s.whatsapp.net")
    assert not is_super_admin("22393056936", "22370000000@c.us")
    assert not is_super_admin("", "22393056936@c.us")  # unset: nobody commands Jeli
    assert not is_super_admin("22393056936", "")


def test_a_sentence_becomes_a_setting_jeli_really_applies():
    model = Model({"key": "answer_engine", "value": "backup", "reply": "C'est fait, je passe sur le secours."})
    admin = Admin(runtime(), model)
    reply = asyncio.run(admin.handle("passe sur le moteur de secours"))
    assert reply == "C'est fait, je passe sur le secours."
    assert admin.runtime["answer_engine"] == "backup"
    # The model is told which settings exist and what each one takes.
    _, system = model.asked[0]
    assert "answer_engine: one of auto, gemini, backup" in system
    assert "member_daily_limit: a number between 0 and 200" in system


def test_a_value_the_setting_does_not_accept_is_refused_not_applied():
    admin = Admin(runtime(), Model({"key": "member_daily_limit", "value": "900", "reply": "ok"}))
    reply = asyncio.run(admin.handle("limite chacun à 900 par jour"))
    assert "member_daily_limit" in reply and "200" in reply
    assert admin.runtime["member_daily_limit"] == 40  # unchanged


def test_the_settings_that_decide_who_jeli_listens_to_are_never_changed_by_message():
    """A misheard word there is not a setting to undo but an incident."""
    admin = Admin(runtime(), Model({"key": "groups", "value": "1203@g.us", "reply": "ok"}))
    assert asyncio.run(admin.handle("ajoute ce groupe")) is None
    assert admin.runtime["groups"] == []


def test_ordinary_talk_is_answered_as_ordinary_talk():
    admin = Admin(runtime(), Model({"action": "none", "reply": "ignored"}))
    assert asyncio.run(admin.handle("bonsoir jeli, ça va ?")) is None


def test_an_action_runs_the_task_at_once():
    class Activity:
        def __init__(self):
            self.ran = []

        def run_now(self, who):
            self.ran.append(who)

    memory = Activity()
    admin = Admin(runtime(), Model({"action": "memory", "reply": "Je m'en occupe."}), {"memory": memory})
    assert asyncio.run(admin.handle("indexe ce qui vient d'être dit")) == "Je m'en occupe."
    assert memory.ran == ["super admin"]


def test_without_a_model_nothing_is_commanded():
    assert asyncio.run(Admin(runtime(), None).handle("mets-toi en pause")) is None


def test_the_app_starts_with_whatsapp_configured(monkeypatch):
    """It crashed in production on 23 Sep at 01:46: the super admin was wired before the
    activities existed, and no test had WhatsApp configured, so nothing caught it."""
    from fastapi.testclient import TestClient

    from app.adapters.whatsapp_waha import Waha
    from app.config import get_settings
    from app.main import app

    monkeypatch.setenv("WAHA_URL", "http://waha.test:3000")
    monkeypatch.setenv("WAHA_API_KEY", "k")
    monkeypatch.setenv("WAHA_WEBHOOK_HMAC_KEY", "h")
    monkeypatch.setenv("SUPER_ADMIN_NUMBER", "22300000000")
    get_settings.cache_clear()

    async def no_status_check(self):
        pass

    monkeypatch.setattr(Waha, "sync_status", no_status_check)
    with TestClient(app) as client:
        assert client.get("/health").json()["whatsapp"] is True
        assert app.state.whatsapp is not None


def test_the_super_admin_can_ask_for_the_two_group_messages_out_loud():
    """Said by voice: the note is listened to first, so it reaches this as plain words."""
    posted = []

    async def greet(which):
        posted.append(which)
        return "sent"

    admin = Admin(runtime(), Model({"action": "hello", "reply": "Je me présente au groupe."}), greet=greet)
    said = asyncio.run(admin.handle("Bienvenue Jeli sur le groupe cohorte, présente-toi"))
    assert said == "Je me présente au groupe." and posted == ["hello"]

    admin = Admin(runtime(), Model({"action": "goodbye", "reply": ""}), greet=greet)
    assert "adieux" in asyncio.run(admin.handle("Jeli, ta période de test est terminée"))
    assert posted == ["hello", "goodbye"]


def test_a_greeting_already_posted_is_never_repeated_on_a_second_command():
    async def greet(which):
        return "already sent"

    admin = Admin(runtime(), Model({"action": "hello", "reply": "ok"}), greet=greet)
    assert "déjà fait" in asyncio.run(admin.handle("présente-toi au groupe"))


def test_only_the_super_admin_is_obeyed_in_a_group():
    """In a group the command must be addressed to Jeli, and come from that one number."""
    from app.adapters.whatsapp_waha import Waha
    from app.config import Settings

    async def respond(message):
        return None

    waha = Waha(Settings(waha_url="http://waha.test", waha_api_key="k", super_admin_number="22393056936"), respond)
    assert waha.super_admin_number == "22393056936"
    assert is_super_admin(waha.super_admin_number, "22393056936@lid") is True
    assert is_super_admin(waha.super_admin_number, "22370000000@c.us") is False
