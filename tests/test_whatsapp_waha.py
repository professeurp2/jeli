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
from app.answer.language import FEELS, TEXTS
from app.answer.react import is_correction
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
    monkeypatch.setattr(Waha, "_put", fake_post)  # reactions: PUT /api/reaction
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

    # WAHA's recommended human-like sequence: seen, typing, stop typing, then send. Having to say
    # it cannot answer yet, Jeli puts the feeling on the member's message first (app/answer/language.py).
    paths = [path for path, _ in calls]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/stopTyping", "/api/reaction", "/api/sendText"]
    assert calls[3][1]["reaction"] == FEELS["not_ready"]
    _, reply = calls[4]
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
    assert "short breather" in texts[2]["text"] or "petite pause" in texts[2]["text"]


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


# --- Reactions (the emotion itself is felt by a model: see tests/test_emotion.py) ---

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


class FakeEmotions:
    """Feels as the model would, from a table: text → (emotion, strength, reaction)."""

    def __init__(self, table):
        self.table, self.felt = table, []

    async def feel(self, text="", image=None, mimetype=""):
        from app.answer.emotion import Feeling

        self.felt.append(text)
        emotion, strength, reaction = self.table.get(text, ("neutral", 0, ""))
        return Feeling(emotion=emotion, strength=strength, reaction=reaction)


def test_jeli_answers_strong_emotion_in_the_group_with_one_gesture(waha_env, calls):
    """Sad news or a loud laugh gets an answer — and exactly one of them.

    Measured 24 September: a sticker AND an emoji went on the same message, which is Jeli saying
    the same thing twice and reads as a machine doing both because it can. Humour and sadness are
    both feelings it has pictures for, so the picture goes and the emoji stays home.
    """
    emotions = FakeEmotions({"haha trop drôle 😂": ("humor", 3, "😂"), "Notre ami est décédé hier.": ("sadness", 3, "😢")})
    with TestClient(app) as client:
        app.state.whatsapp.emotions = emotions
        post_event(client, message_event("haha trop drôle 😂", message_id="m1"))
        post_event(client, message_event("Notre ami est décédé hier.", message_id="m2"))
    reactions = [(p["messageId"], p["reaction"]) for path, p in calls if path == "/api/reaction"]
    stickers = [p for path, p in calls if path == "/api/sendSticker"]
    assert len(stickers) == 2 and reactions == []
    # Two feelings, two different pictures: a feeling is not a single fixed image.
    drawn = {str(p["file"].get("data"))[-80:] for p in stickers if isinstance(p.get("file"), dict)}
    assert len(drawn) == 2, "both feelings were answered with the same sticker"


def test_jeli_does_not_react_to_ordinary_chatter_or_thanks_in_the_group(waha_env, calls):
    """No reaction to a neutral message, nor to a "thanks" or a "hello" between members."""
    emotions = FakeEmotions({"Merci à tous !": ("gratitude", 2, "🙏")})
    with TestClient(app) as client:
        app.state.whatsapp.emotions = emotions
        post_event(client, message_event("The pitch deck is due Friday.", message_id="m1"))
        post_event(client, message_event("Merci à tous !", message_id="m2"))
    assert emotions.felt == ["The pitch deck is due Friday.", "Merci à tous !"]
    assert not any(path == "/api/reaction" for path, _ in calls)


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
    assert "/api/reaction" in paths
    assert "/api/deleteMessage" in paths

    react = next((p for path, p in calls if path == "/api/reaction"), None)
    assert react["reaction"] == "🙏"
    assert react["messageId"] == "correction-1"

    delete = next((p for path, p in calls if path == "/api/deleteMessage"), None)
    assert delete["messageId"] == wrong_message_id


def test_a_member_past_their_share_of_the_day_is_told_once_then_answered_in_writing(waha_env, calls, monkeypatch):
    """The barrier never silences anyone: it says so once, kindly, and drops the voice note."""
    monkeypatch.setenv("MEMBER_DAILY_LIMIT", "2")
    monkeypatch.setenv("WHATSAPP_USER_LIMIT", "20")  # the 10-minute anti-ban rule is tested elsewhere
    get_settings.cache_clear()

    async def respond(message):
        return "Friday at 10."

    with TestClient(app) as client:
        app.state.whatsapp.respond = respond
        # A voice that always speaks: it must fall silent once the member is past their share.
        spoken_notes = []

        class AlwaysSpeaks:
            async def speak(self, text, language=""):
                spoken_notes.append(text)
                return b"audio"

        app.state.whatsapp.voice = AlwaysSpeaks()
        app.state.whatsapp.voice_rate = app.state.whatsapp.voice_intro_rate = 1.0
        for i in range(3):
            event = message_event(f"@{BOT_PHONE} question number {i}?", message_id=f"m{i}")
            assert post_event(client, event).status_code == 200

    said = [payload for path, payload in calls if path == "/api/sendText"]
    reactions = [payload for path, payload in calls if path == "/api/reaction"]
    # The first two answers say nothing about any limit; the third explains, then answers.
    texts = [t["text"] for t in said]
    assert texts.count(TEXTS["en"]["daily_cap"]) == 1
    assert texts[-2:] == [TEXTS["en"]["daily_cap"], "Friday at 10."]
    assert len(reactions) == 1  # the warmth goes with the sentence, once
    assert len(spoken_notes) == 2  # the first two answers were spoken, the third is written only
    # A fourth message is answered too, without repeating the sentence.
    calls.clear()
    with TestClient(app) as client:
        app.state.whatsapp.respond = respond
        app.state.whatsapp.member_daily_limit = 2
        app.state.whatsapp.daily_limiter.limit = 2
        for i in range(3):
            later = message_event(f"@{BOT_PHONE} another question {i}?", message_id=f"n{i}")
            assert post_event(client, later).status_code == 200
    assert TEXTS["en"]["daily_cap"] not in [t["text"] for t in calls if isinstance(t, dict)]


def test_without_a_limit_nothing_changes(waha_env, calls, monkeypatch):
    monkeypatch.setenv("MEMBER_DAILY_LIMIT", "0")
    monkeypatch.setenv("WHATSAPP_USER_LIMIT", "20")
    get_settings.cache_clear()

    async def respond(message):
        return "Friday at 10."

    with TestClient(app) as client:
        app.state.whatsapp.respond = respond
        for i in range(5):
            asked = message_event(f"@{BOT_PHONE} yet another question {i}?", message_id=f"z{i}")
            assert post_event(client, asked).status_code == 200
    assert [p["text"] for path, p in calls if path == "/api/sendText"] == ["Friday at 10."] * 5


def test_the_account_id_is_read_whatever_shape_whatsapp_answers_with():
    """Measured 23 Sep: WAHA answered 200 to /lids/pn/… and the id was not where we looked."""
    from app.adapters.whatsapp_waha import _lid_in

    assert _lid_in({"lid": "123456789012345@lid"}) == "123456789012345@lid"
    assert _lid_in({"id": {"_serialized": "123@lid"}}) == "123@lid"
    assert _lid_in([{"lid": "987@lid"}]) == "987@lid"
    assert _lid_in("123456789012345@lid") == "123456789012345@lid"
    assert _lid_in("123456789012345") == "123456789012345"
    # Nothing found is nothing claimed: a 200 with an empty body is not an id.
    assert _lid_in({}) == "" and _lid_in(None) == "" and _lid_in({"lid": None}) == ""
    assert _lid_in({"pn": "22300000000@c.us"}) == ""  # the number it was asked about, not an id


def test_every_page_of_numbers_is_learned_not_just_the_first():
    """Measured 23 Sep: the first page came back exactly full — there were more, and the member
    missing from it is the one who cannot sign in."""
    import asyncio

    from app.answer.citations import NUMBER_OF_LID
    from app.config import Settings
    from app.adapters.whatsapp_waha import Waha

    pages = {0: [{"lid": f"{n}@lid", "pn": f"2239{n:07d}@c.us"} for n in range(100)],
             100: [{"lid": "9999@lid", "pn": "22399999999@c.us"}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/groups"):
            return httpx.Response(200, json=[])
        offset = int(request.url.params.get("offset", 0))
        return httpx.Response(200, json=pages.get(offset, []))

    async def respond(message):
        return None

    waha = Waha(Settings(waha_url="http://waha.test", waha_api_key="k"), respond)
    waha._http = httpx.AsyncClient(base_url="http://waha.test", transport=httpx.MockTransport(handler))
    NUMBER_OF_LID.clear()
    try:
        assert asyncio.run(waha.learn_numbers(limit=100)) == 101
        assert NUMBER_OF_LID["9999"] == "22399999999"  # the one on the second page
    finally:
        NUMBER_OF_LID.clear()


def test_what_a_voice_note_cannot_carry_follows_it_in_writing(waha_env, calls, monkeypatch):
    """Measured 23 Sep at 21:34: a member asked Jeli for its call link and got a 44-second voice
    note with no link in it. The spoken version strips links by design, and nothing sent them
    after — so the answer's whole point was lost."""
    monkeypatch.setenv("WHATSAPP_USER_LIMIT", "20")
    get_settings.cache_clear()
    answer = "Bien sûr, appelle-moi ici : https://jeli.example.org/jeli/call\n> *Diane* · mar. 22 sept."

    async def respond(message):
        return answer

    spoken_texts = []

    class Speaks:
        async def speak(self, text, language=""):
            spoken_texts.append(text)
            return b"audio"

    with TestClient(app) as client:
        app.state.whatsapp.respond = respond
        app.state.whatsapp.voice = Speaks()
        app.state.whatsapp.voice_rate = app.state.whatsapp.voice_intro_rate = 1.0
        assert post_event(client, message_event(f"@{BOT_PHONE} send me your call link")).status_code == 200

    paths = [path for path, _ in calls]
    assert "/api/sendVoice" in paths and "/api/sendText" in paths
    assert "https://" not in spoken_texts[0]  # the voice never reads a link out
    written = next(p["text"] for path, p in calls if path == "/api/sendText")
    assert "https://jeli.example.org/jeli/call" in written and "Diane" in written


# --- Who may steer Jeli -------------------------------------------------------------------------


def _member(text, author_id="266000696094755@lid", author="Awa", chat="120363@g.us"):
    from datetime import datetime, timezone

    from app.models import IncomingMessage

    return IncomingMessage(
        platform="whatsapp", chat_id=chat, message_id="m1", author=author, text=text,
        sent_at=datetime(2026, 9, 24, 9, tzinfo=timezone.utc), is_private=False,
        addressed_to_bot=True, author_id=author_id,
    )


def test_no_message_can_switch_jeli_off():
    """Measured 24 September, in the cohort group: a member wrote "/pause" and Jeli stopped
    answering all 240 of them. The commands are gone rather than repaired — pausing, resuming and
    silencing a group are the dashboard's job, where signing in is what proves who you are."""
    import inspect

    from app.adapters import whatsapp_waha

    source = inspect.getsource(whatsapp_waha)
    for gone in ("ADMIN_COMMAND", "ADMIN_DURATION", "_try_admin_command", "_is_admin("):
        assert gone not in source, gone
    assert not hasattr(whatsapp_waha, "ADMIN_COMMAND")


def test_the_super_admin_is_recognised_by_number_never_by_an_account_id():
    """The check that failed compared the sender against the sender, so every member passed it.
    This one compares the configured number against the phone numbers the sender is really known
    by — and an account id, a long run of digits meaning nothing outside WhatsApp, is not one."""
    from app.answer.citations import NUMBER_OF_LID

    channel = Waha.__new__(Waha)
    # A LID alone says nothing: not the number, and not a candidate to be matched against one.
    assert channel._known_numbers(_member("hi", author_id="266000696094755@lid")) == []
    # A private chat id is the number itself.
    assert channel._known_numbers(_member("hi", author_id="22393056936@c.us")) == ["22393056936"]
    # And a LID WhatsApp has translated for us gives the number behind it.
    NUMBER_OF_LID["266000696094755"] = "22393056936"
    try:
        assert channel._known_numbers(_member("hi", author_id="266000696094755@lid")) == ["22393056936"]
    finally:
        NUMBER_OF_LID.pop("266000696094755", None)


def test_a_lid_that_merely_ends_like_the_admins_number_is_not_the_admin():
    """Admin numbers are matched on their ending, because a number may or may not carry its
    country code. Matched against an account id, that would hand the group to a stranger."""
    from app.control.admin import is_super_admin

    channel = Waha.__new__(Waha)
    pretender = _member("mets-toi en pause", author_id="99999922393056936@lid")
    # The old comparison would have said yes, on an id that is not a phone number at all.
    assert is_super_admin("22393056936", "99999922393056936@lid") is True
    # The new one never gets the chance: that id is not a number the sender is known by.
    assert not any(is_super_admin("22393056936", k) for k in channel._known_numbers(pretender))


def test_nobody_steers_jeli_when_no_number_is_configured():
    from app.control.admin import is_super_admin

    assert is_super_admin("", "22393056936@c.us") is False
    assert is_super_admin("   ", "22393056936@c.us") is False
