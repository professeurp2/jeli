import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.adapters.whatsapp_waha import WEBHOOK_PATH, Waha, parse_message, verify_signature
from app.config import get_settings
from app.main import app

HMAC_KEY = "test-hmac-key"
GROUP = "120363000000000000@g.us"
OTHER_GROUP = "120363999999999999@g.us"
BOT_PHONE = "22380000000"
BOT_LID = "98765432100000"
AWA = "22370000000@c.us"


def message_event(body, chat_id=GROUP, message_id="false_120363000000000000@g.us_AAA_22370000000@c.us", **payload):
    return {
        "event": "message",
        "session": "default",
        "me": {"id": f"{BOT_PHONE}@c.us", "pushName": "Jeli", "lid": f"{BOT_LID}@lid"},
        "payload": {
            "id": message_id,
            "timestamp": 1789725600,
            "from": chat_id,
            "fromMe": False,
            "participant": AWA if chat_id.endswith("@g.us") else None,
            "body": body,
            "hasMedia": False,
            "_data": {"Info": {"PushName": "Awa Traoré", "IsGroup": chat_id.endswith("@g.us")}},
            **payload,
        },
        "engine": "GOWS",
    }


def sign(body: bytes) -> str:
    return hmac.new(HMAC_KEY.encode(), body, hashlib.sha512).hexdigest()


def test_signature_matches_waha_documentation_example():
    body = b'{"event":"message","session":"default","engine":"WEBJS"}'
    expected = (
        "208f8a55dde9e05519e898b10b89bf0d0b3b0fdf11fdbf09b6b90476301b98d8"
        "097c462b2b17a6ce93b6b47a136cf2e78a33a63f6752c2c1631777076153fa89"
    )
    assert verify_signature(body, expected, "my-secret-key")
    assert not verify_signature(body + b" ", expected, "my-secret-key")
    assert not verify_signature(body, None, "my-secret-key")
    assert not verify_signature(body, expected, "")


def test_plain_group_message_is_not_addressed_to_bot():
    message = parse_message(message_event("Is the bootcamp still on Monday?"), "Jeli")
    assert message is not None
    assert not message.addressed_to_bot
    assert not message.is_private
    assert message.author == "Awa Traoré"
    assert message.chat_id == GROUP


def test_mention_in_text_addresses_bot_and_is_stripped():
    message = parse_message(message_event(f"@{BOT_PHONE} what was decided about the dates?"), "Jeli")
    assert message.addressed_to_bot
    assert message.text == "what was decided about the dates?"


def test_mention_by_hidden_lid_in_raw_data_addresses_bot():
    event = message_event(f"@{BOT_LID} any news?")
    event["payload"]["_data"]["Message"] = {
        "extendedTextMessage": {"text": f"@{BOT_LID} any news?", "contextInfo": {"mentionedJID": [f"{BOT_LID}@lid"]}}
    }
    message = parse_message(event, "Jeli")
    assert message.addressed_to_bot
    assert message.text == "any news?"


def test_mention_inside_a_quoted_message_does_not_count():
    event = message_event("I agree")
    event["payload"]["_data"]["Message"] = {
        "extendedTextMessage": {
            "text": "I agree",
            "contextInfo": {"quotedMessage": {"extendedTextMessage": {"contextInfo": {"mentionedJID": [f"{BOT_LID}@lid"]}}}},
        }
    }
    assert not parse_message(event, "Jeli").addressed_to_bot


def test_reply_to_a_bot_message_addresses_bot():
    event = message_event("and the deadline?", replyTo={"id": "AAA", "participant": f"{BOT_LID}@lid", "body": "Hello"})
    assert parse_message(event, "Jeli").addressed_to_bot


def test_bot_name_at_the_start_addresses_bot():
    message = parse_message(message_event("Jeli, when is the deadline?"), "Jeli")
    assert message.addressed_to_bot
    assert message.text == "when is the deadline?"
    assert not parse_message(message_event("Jelili is presenting today"), "Jeli").addressed_to_bot


def test_commands_address_bot():
    assert parse_message(message_event("/catchup since Monday"), "Jeli").addressed_to_bot


def test_direct_message_addresses_bot():
    message = parse_message(message_event("hi", chat_id=AWA), "Jeli")
    assert message.addressed_to_bot
    assert message.is_private


@pytest.mark.parametrize(
    "event",
    [
        message_event("my own message", fromMe=True),
        message_event("status", chat_id="status@broadcast"),
        message_event("", hasMedia=True),
        {**message_event("hi"), "event": "message.ack"},
    ],
)
def test_ignored_events(event):
    assert parse_message(event, "Jeli") is None


@pytest.fixture
def waha_env(monkeypatch):
    monkeypatch.setenv("WAHA_URL", "http://waha.test:3000")
    monkeypatch.setenv("WAHA_API_KEY", "test-api-key")
    monkeypatch.setenv("WAHA_WEBHOOK_HMAC_KEY", HMAC_KEY)
    monkeypatch.setenv("WHATSAPP_GROUP_IDS", GROUP)
    get_settings.cache_clear()


@pytest.fixture
def calls(monkeypatch):
    """Capture the calls to WAHA instead of sending them."""
    captured = []

    async def fake_post(self, path, payload):
        captured.append((path, payload))

    monkeypatch.setattr(Waha, "_post", fake_post)
    return captured


def post_event(client, event):
    body = json.dumps(event).encode()
    return client.post(WEBHOOK_PATH, content=body, headers={"Content-Type": "application/json", "X-Webhook-Hmac": sign(body)})


def test_webhook_rejects_unsigned_events(waha_env, calls):
    with TestClient(app) as client:
        response = client.post(WEBHOOK_PATH, json=message_event(f"@{BOT_PHONE} hi"))
    assert response.status_code == 403
    assert calls == []


def test_webhook_answers_a_mention_once(waha_env, calls):
    event = message_event(f"@{BOT_PHONE} what did I miss?")
    with TestClient(app) as client:
        assert client.get("/health").json()["whatsapp"] is True
        assert post_event(client, event).status_code == 200
        assert post_event(client, event).status_code == 200  # retried delivery

    paths = [path for path, _ in calls]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/sendText", "/api/stopTyping"]
    _, reply = calls[2]
    assert reply["chatId"] == GROUP
    assert reply["reply_to"] == event["payload"]["id"]
    assert "Awa Traoré" in reply["text"]


def test_webhook_stays_silent_on_ordinary_group_chatter(waha_env, calls):
    with TestClient(app) as client:
        assert post_event(client, message_event("see you tomorrow")).status_code == 200
    assert calls == []


def test_webhook_ignores_groups_not_allowed(waha_env, calls):
    with TestClient(app) as client:
        response = post_event(client, message_event(f"@{BOT_PHONE} hi", chat_id=OTHER_GROUP))
    assert response.status_code == 200
    assert calls == []
