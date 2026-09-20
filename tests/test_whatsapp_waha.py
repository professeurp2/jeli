import asyncio
import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.adapters import whatsapp_waha
from app.adapters.whatsapp_waha import WEBHOOK_PATH, Waha, parse_message, verify_signature
from app.answer.language import TEXTS
from app.answer.react import emotion_emoji, is_correction
from app.config import Settings, get_settings
from app.main import app

HMAC_KEY = "test-hmac-key"
GROUP = "120363000000000000@g.us"
OTHER_GROUP = "120363999999999999@g.us"
BOT_PHONE = "22380000000"
BOT_LID = "98765432100000"
AWA = "22370000000@c.us"


def message_event(
    body, chat_id=GROUP, message_id="false_120363000000000000@g.us_AAA_22370000000@c.us", age_seconds=5, **payload
):
    return {
        "event": "message",
        "session": "default",
        "me": {"id": f"{BOT_PHONE}@c.us", "pushName": "Jeli", "lid": f"{BOT_LID}@lid"},
        "payload": {
            "id": message_id,
            "timestamp": int(time.time()) - age_seconds,
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
    assert message.author_id == AWA
    assert message.chat_id == GROUP


def test_mention_in_text_addresses_bot_and_is_stripped():
    message = parse_message(message_event(f"@{BOT_PHONE} what was decided about the dates?"), "Jeli")
    assert message.addressed_to_bot
    assert message.text == "what was decided about the dates?"


def test_display_name_mention_is_stripped():
    """GOWS engine puts '@Jeli_bot' in the body instead of the number."""
    event = message_event("@Jeli_bot what time is the session?")
    event["payload"]["_data"]["Message"] = {
        "extendedTextMessage": {"contextInfo": {"mentionedJID": [f"{BOT_LID}@lid"]}}
    }
    message = parse_message(event, "Jeli")
    assert message is not None
    assert message.addressed_to_bot
    assert message.text == "what time is the session?"


def test_bare_mention_with_quoted_context_uses_quoted_body():
    """'@Jeli_bot' alone (empty question) with a quoted message → quoted body becomes the question."""
    event = message_event("@Jeli_bot")
    event["payload"]["_data"]["Message"] = {
        "extendedTextMessage": {"contextInfo": {"mentionedJID": [f"{BOT_LID}@lid"]}}
    }
    event["payload"]["replyTo"] = {
        "id": "quoted-msg-1",
        "participant": "22300000001@c.us",
        "body": "Team declarations due: close of business, Thursday 17 September 2026.",
    }
    message = parse_message(event, "Jeli")
    assert message is not None
    assert message.addressed_to_bot
    assert message.text == "Team declarations due: close of business, Thursday 17 September 2026."


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
    # Human-like delays are tested in test_pacing.py; here they would only slow the suite down.
    monkeypatch.setenv("WHATSAPP_MIN_SEND_INTERVAL_SECONDS", "0")
    monkeypatch.setattr(whatsapp_waha, "reading_delay", lambda: 0)
    monkeypatch.setattr(whatsapp_waha, "typing_duration", lambda text: 0)

    async def no_status_check(self):
        pass

    monkeypatch.setattr(Waha, "sync_status", no_status_check)
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

    # WAHA's recommended human-like sequence: seen, typing, stop typing, then send.
    paths = [path for path, _ in calls]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/stopTyping", "/api/sendText"]
    _, reply = calls[3]
    assert reply["chatId"] == GROUP
    assert reply["reply_to"] == event["payload"]["id"]
    # No knowledge base in tests: Jeli says it isn't ready, in the question's language.
    assert reply["text"] == TEXTS["en"]["not_ready"]


def test_webhook_stays_silent_on_ordinary_group_chatter(waha_env, calls):
    with TestClient(app) as client:
        assert post_event(client, message_event("see you tomorrow")).status_code == 200
    assert calls == []


def test_jeli_steps_in_when_the_responder_finds_an_earlier_answer(waha_env, calls):
    async def respond(message):
        return None if message.addressed_to_bot else "💡 This was already answered in the group: 24 September."

    with TestClient(app) as client:
        app.state.whatsapp.respond = respond
        post_event(client, message_event("When is the hackathon deadline?"))
    paths = [path for path, _ in calls]
    # No read receipt: nobody asked Jeli. Typing, then the reply quoting the question.
    assert paths == ["/api/startTyping", "/api/stopTyping", "/api/sendText"]
    assert calls[-1][1]["text"].startswith("💡")


def test_group_chatter_is_remembered_once(waha_env, calls):
    remembered = []

    async def ingest(message):
        remembered.append(message)

    event = message_event("The pitch deck is due Friday.\nSlides in English please.")
    with TestClient(app) as client:
        app.state.whatsapp.ingest = ingest
        post_event(client, event)
        post_event(client, event)  # retried delivery
    [message] = remembered
    assert message.text == "The pitch deck is due Friday.\nSlides in English please."
    assert calls == []


def test_webhook_ignores_groups_not_allowed(waha_env, calls):
    with TestClient(app) as client:
        response = post_event(client, message_event(f"@{BOT_PHONE} hi", chat_id=OTHER_GROUP))
    assert response.status_code == 200
    assert calls == []


def sent_texts(calls):
    return [payload for path, payload in calls if path == "/api/sendText"]


def test_old_messages_delivered_after_a_reconnection_are_not_answered(waha_env, calls):
    with TestClient(app) as client:
        post_event(client, message_event(f"@{BOT_PHONE} still there?", age_seconds=3600))
    assert calls == []


def test_a_member_cannot_make_jeli_flood_the_group(waha_env, calls, monkeypatch):
    monkeypatch.setenv("WHATSAPP_USER_LIMIT", "2")
    get_settings.cache_clear()
    with TestClient(app) as client:
        for n in range(3):
            post_event(client, message_event(f"@{BOT_PHONE} question {n}", message_id=f"msg-{n}"))
        # Another member still gets an answer.
        other = message_event(f"@{BOT_PHONE} my question", message_id="msg-other")
        other["payload"]["participant"] = "22371111111@c.us"
        post_event(client, other)
    texts = sent_texts(calls)
    assert len(texts) == 4  # msg-0, msg-1 answered; msg-2 gets the flood explanation; msg-other answered
    assert "short break" in texts[2]["text"] or "peu de temps" in texts[2]["text"]


def make_waha(handler):
    settings = Settings(_env_file=None, waha_url="http://waha.test:3000", waha_api_key="key", waha_webhook_hmac_key="h")
    waha = Waha(settings, respond=None)
    waha._http = httpx.AsyncClient(base_url=settings.waha_url, transport=httpx.MockTransport(handler))
    return waha


def test_session_status_is_read_from_waha_at_startup():
    def handler(request):
        assert request.url.path == "/api/sessions/default"
        return httpx.Response(200, json={"name": "default", "status": "SCAN_QR_CODE"})

    waha = make_waha(handler)
    asyncio.run(waha.sync_status())
    assert waha.paused


def test_unreachable_waha_at_startup_does_not_crash_jeli():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    waha = make_waha(handler)
    asyncio.run(waha.sync_status())
    assert not waha.paused


def test_jeli_stays_silent_while_the_session_is_down(waha_env, calls):
    def status(value):
        return {"event": "session.status", "session": "default", "payload": {"status": value}}

    with TestClient(app) as client:
        post_event(client, status("FAILED"))
        post_event(client, message_event(f"@{BOT_PHONE} hello?", message_id="during-outage"))
        post_event(client, status("WORKING"))
        post_event(client, message_event(f"@{BOT_PHONE} hello again", message_id="after-recovery"))
    [reply] = sent_texts(calls)
    assert reply["reply_to"] == "after-recovery"


def test_private_messages_go_only_to_numbers_on_whatsapp(monkeypatch):
    monkeypatch.setattr(whatsapp_waha, "typing_duration", lambda text: 0)
    sent = []

    def handler(request):
        if request.url.path == "/api/contacts/check-exists":
            phone = request.url.params["phone"]
            found = phone == "2347069310683"
            return httpx.Response(200, json={"numberExists": found, "chatId": f"{phone}@c.us" if found else None})
        if request.url.path == "/api/sendText":
            sent.append(json.loads(request.content))
        return httpx.Response(200, json={})

    waha = make_waha(handler)
    waha.spacer = whatsapp_waha.SendSpacer(0)
    assert asyncio.run(waha.post_private("2347069310683", "Weekly report"))
    assert not asyncio.run(waha.post_private("2340000000000", "Weekly report"))
    assert [(m["chatId"], m["text"]) for m in sent] == [("2347069310683@c.us", "Weekly report")]


# --- Emoji reaction tests ---

def test_emotion_emoji_detects_funny_messages():
    assert emotion_emoji("hahaha c'est trop drôle 😂") == "😄"
    assert emotion_emoji("MDR j'y crois pas") == "😄"
    assert emotion_emoji("lol that was unexpected 🤣") == "😄"


def test_emotion_emoji_detects_sad_messages():
    assert emotion_emoji("Notre collègue est décédé hier soir 😢") == "😢"
    assert emotion_emoji("He passed away this morning. RIP") == "😢"
    assert emotion_emoji("Condoléances à sa famille") == "😢"


def test_emotion_emoji_returns_none_for_neutral_messages():
    assert emotion_emoji("The deadline is on Friday.") is None
    assert emotion_emoji("When does registration close?") is None


def test_sad_takes_priority_over_funny():
    # A sad message that also contains 😂 is still treated as sad.
    assert emotion_emoji("He passed away 😂") == "😢"


def test_is_correction_detects_corrections_in_french():
    assert is_correction("Jeli tu t'es trompé, c'est pas ça")
    assert is_correction("C'est faux ce que tu as dit")
    assert is_correction("Non Jeli, mauvaise réponse")


def test_is_correction_detects_corrections_in_english():
    assert is_correction("That's wrong, the date is the 25th")
    assert is_correction("You made a mistake, jeli")
    assert is_correction("Incorrect answer!")


def test_is_correction_ignores_neutral_messages():
    assert not is_correction("When is the deadline?")
    assert not is_correction("Thank you Jeli!")


def test_jeli_reacts_to_funny_group_messages(waha_env, calls):
    """A funny message in the group gets a 😄 reaction from Jeli."""
    event = message_event("haha trop drôle 😂")
    with TestClient(app) as client:
        post_event(client, event)
    reactions = [(path, payload) for path, payload in calls if path == "/api/sendReaction"]
    assert len(reactions) == 1
    _, react_payload = reactions[0]
    assert react_payload["reaction"] == "😄"
    assert react_payload["messageId"] == event["payload"]["id"]


def test_jeli_reacts_to_sad_group_messages(waha_env, calls):
    """A sad message (condolences/death) gets a 😢 reaction."""
    event = message_event("Notre ami est décédé hier. Condoléances à sa famille.")
    with TestClient(app) as client:
        post_event(client, event)
    reactions = [p for path, p in calls if path == "/api/sendReaction"]
    assert len(reactions) == 1
    assert reactions[0]["reaction"] == "😢"


def test_jeli_does_not_react_to_ordinary_chatter(waha_env, calls):
    """No reaction to a neutral message."""
    with TestClient(app) as client:
        post_event(client, message_event("The pitch deck is due Friday."))
    assert not any(path == "/api/sendReaction" for path, _ in calls)


def test_correction_triggers_reaction_and_deletes_wrong_message(waha_env, calls):
    """When someone corrects Jeli, it reacts 🙏 and tries to delete its wrong message."""
    wrong_message_id = "jeli-sent-msg-001"

    async def respond_then_correct(message):
        return "The bootcamp is on Monday." if message.addressed_to_bot else None

    with TestClient(app) as client:
        waha = app.state.whatsapp
        waha.respond = respond_then_correct
        # Jeli replies to a question; fake the stored sent message ID directly.
        post_event(client, message_event(f"@{BOT_PHONE} when is the bootcamp?", message_id="q1"))
        waha._last_sent[GROUP] = wrong_message_id
        # Someone corrects Jeli.
        post_event(client, message_event(f"@{BOT_PHONE} c'est faux, c'est mardi", message_id="correction-1"))

    paths = [path for path, _ in calls]
    assert "/api/sendReaction" in paths
    assert "/api/deleteMessage" in paths

    react = next((p for path, p in calls if path == "/api/sendReaction"), None)
    assert react["reaction"] == "🙏"
    assert react["messageId"] == "correction-1"

    delete = next((p for path, p in calls if path == "/api/deleteMessage"), None)
    assert delete["messageId"] == wrong_message_id
