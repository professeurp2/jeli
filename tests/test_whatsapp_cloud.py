import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.adapters.whatsapp_cloud import WEBHOOK_PATH, WhatsAppCloud, parse_messages, verify_signature
from app.config import get_settings
from app.main import app

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


def text_payload(body="What was decided about the bootcamp dates?", message_id="wamid.ABC"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15550001111", "phone_number_id": "PHONE_ID"},
                            "contacts": [{"profile": {"name": "Awa Traoré"}, "wa_id": "22370000000"}],
                            "messages": [
                                {
                                    "from": "22370000000",
                                    "id": message_id,
                                    "timestamp": "1789725600",
                                    "type": "text",
                                    "text": {"body": body},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def status_payload():
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [{"id": "wamid.OUT", "status": "delivered", "recipient_id": "22370000000"}],
                        },
                    }
                ],
            }
        ],
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def whatsapp_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-access-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "PHONE_ID")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    get_settings.cache_clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture the calls to the Graph API instead of sending them."""
    calls = []

    async def fake_post(self, payload):
        calls.append(payload)

    monkeypatch.setattr(WhatsAppCloud, "_post", fake_post)
    return calls


def test_signature_matches_only_the_exact_body():
    body = b'{"hello": "world"}'
    assert verify_signature(body, sign(body), APP_SECRET)
    assert not verify_signature(body + b" ", sign(body), APP_SECRET)
    assert not verify_signature(body, None, APP_SECRET)
    assert not verify_signature(body, sign(body), "")


def test_parse_text_message():
    [message] = parse_messages(text_payload())
    assert message.platform == "whatsapp"
    assert message.chat_id == "22370000000"
    assert message.message_id == "wamid.ABC"
    assert message.author == "Awa Traoré"
    assert message.text == "What was decided about the bootcamp dates?"
    assert message.addressed_to_bot and message.is_private
    assert message.sent_at.year == 2026


def test_parse_ignores_statuses_and_non_text_messages():
    assert parse_messages(status_payload()) == []
    payload = text_payload()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "audio"
    assert parse_messages(payload) == []


def test_webhook_verification(whatsapp_env):
    with TestClient(app) as client:
        ok = client.get(
            WEBHOOK_PATH,
            params={"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444"},
        )
        wrong = client.get(
            WEBHOOK_PATH,
            params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1158201444"},
        )
    assert ok.status_code == 200
    assert ok.text == "1158201444"
    assert wrong.status_code == 403


def test_webhook_rejects_unsigned_payloads(whatsapp_env, sent):
    with TestClient(app) as client:
        response = client.post(WEBHOOK_PATH, json=text_payload())
    assert response.status_code == 403
    assert sent == []


def test_webhook_answers_a_direct_message_once(whatsapp_env, sent):
    body = json.dumps(text_payload()).encode()
    headers = {"Content-Type": "application/json", "X-Hub-Signature-256": sign(body)}
    with TestClient(app) as client:
        assert client.get("/health").json()["whatsapp"] is True
        first = client.post(WEBHOOK_PATH, content=body, headers=headers)
        retry = client.post(WEBHOOK_PATH, content=body, headers=headers)

    assert first.status_code == 200 and retry.status_code == 200
    read, reply = sent  # the retried delivery must not trigger a second reply
    assert read["status"] == "read" and read["message_id"] == "wamid.ABC"
    assert reply["to"] == "22370000000"
    assert reply["context"] == {"message_id": "wamid.ABC"}
    assert "Awa Traoré" in reply["text"]["body"]
