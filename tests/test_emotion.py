import asyncio
import json

import httpx
import pytest

from app.adapters import whatsapp_waha
from app.adapters.whatsapp_waha import Waha, parse_message
from app.answer.emotion import Emotions, Feeling
from app.answer.llm import LLMUnavailable
from app.config import Settings
from app.models import Reply
from tests.test_whatsapp_waha import AWA, GROUP, message_event


class ModelSays:
    """The model's answer to "how does this message feel?"."""

    def __init__(self, answer):
        self.answer, self.contents = answer, []

    async def generate(self, contents, schema, **kwargs):
        self.contents.append(contents)
        if isinstance(self.answer, Exception):
            raise self.answer
        return schema(**self.answer)


def feel(answer, text="", image=None):
    llm = ModelSays(answer)
    return asyncio.run(Emotions(llm).feel(text, image=image)), llm


def test_the_model_reads_the_emotion_and_picks_a_reaction_jeli_uses():
    feeling, _ = feel({"emotion": "gratitude", "strength": 3, "reaction": "🙏🏾"}, "Merci infiniment Jeli 🙏🏾")
    assert feeling == Feeling(emotion="gratitude", strength=3, reaction="🙏")  # the skin tone is the member's, not a new emoji
    assert feel({"emotion": "love", "strength": 2, "reaction": "❤"}, "❤")[0].reaction == "❤️"
    assert feel({"emotion": "Joy", "strength": 7, "reaction": "🦄"}, "yay")[0] == Feeling(emotion="joy", strength=3, reaction="")
    assert feel({"emotion": "nostalgia", "strength": 1, "reaction": ""}, "hmm")[0].emotion == "neutral"


def test_bad_news_is_never_laughed_at():
    feeling, _ = feel({"emotion": "humor", "strength": 2, "reaction": "😂", "is_bad_news": True}, "He passed away 😂")
    assert feeling.emotion == "sadness" and feeling.reaction == "🙏"


def test_a_sticker_is_looked_at_and_a_failure_costs_nothing():
    feeling, llm = feel({"emotion": "humor", "strength": 2, "reaction": "😂"}, image=b"RIFF-webp")
    part, words = llm.contents[0]
    assert part.inline_data.data == b"RIFF-webp" and part.inline_data.mime_type == "image/webp" and words == "A sticker."
    assert feeling.reaction == "😂"
    assert feel(LLMUnavailable())[0] is None
    assert feel(LLMUnavailable(), "Merci !")[0] is None
    assert feel(ConnectionError("network down"), "Merci !")[0] is None  # a reaction never costs the reply


class Feels:
    def __init__(self, feeling):
        self.feeling, self.seen = feeling, []

    async def feel(self, text="", image=None, mimetype=""):
        self.seen.append((text, image))
        return self.feeling


@pytest.fixture
def waha(monkeypatch):
    monkeypatch.setattr(whatsapp_waha, "reading_delay", lambda: 0)
    monkeypatch.setattr(whatsapp_waha, "typing_duration", lambda text: 0)
    sent = []

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/files/default/sticker.webp"
            return httpx.Response(200, content=b"RIFF-webp")
        sent.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/api/reaction":
            assert request.method == "PUT"  # WAHA's endpoint (POST /api/sendReaction answered 404)
        return httpx.Response(201, json={"id": "jeli-1"})

    settings = Settings(_env_file=None, waha_url="http://waha.test:3000", waha_api_key="key", waha_webhook_hmac_key="h",
                        whatsapp_min_send_interval_seconds=0)

    async def respond(message):
        return Reply("Avec plaisir ! 😊")

    waha = Waha(settings, respond=respond)
    waha._http = httpx.AsyncClient(base_url=settings.waha_url, transport=httpx.MockTransport(handler))
    waha.paused, waha.sent = False, sent
    return waha


def reactions(waha):
    return [(body["messageId"], body["reaction"]) for path, body in waha.sent if path == "/api/reaction"]


STICKER = {"url": "http://localhost:3000/api/files/default/sticker.webp", "mimetype": "image/webp"}


def sticker_event(chat_id=AWA):
    return message_event("", chat_id=chat_id, message_id="sticker-1", hasMedia=True, media=STICKER, type="sticker")


def test_jeli_looks_at_a_sticker_sent_to_it_and_reacts_to_its_feeling(waha):
    waha.emotions = Feels(Feeling(emotion="humor", strength=2, reaction="😂"))
    asyncio.run(waha.handle(parse_message(sticker_event(), "Jeli")))
    assert waha.emotions.seen == [("", b"RIFF-webp")] and reactions(waha) == [("sticker-1", "😂")]
    # A sticker between members, outside any conversation with Jeli: not even looked at.
    waha.emotions, waha.sent[:] = Feels(Feeling(emotion="humor", strength=3, reaction="😂")), []
    waha.in_conversation = lambda message: False
    asyncio.run(waha.handle(parse_message(sticker_event(GROUP), "Jeli")))
    assert waha.emotions.seen == [] and reactions(waha) == []


def test_a_thanks_to_jeli_gets_its_reaction_and_its_reply(waha):
    waha.emotions = Feels(Feeling(emotion="gratitude", strength=2, reaction="❤️"))
    asyncio.run(waha.handle(parse_message(message_event("Merci Jeli ❤️", chat_id=AWA, message_id="m1"), "Jeli")))
    assert reactions(waha) == [("m1", "❤️")]
    assert [body["text"] for path, body in waha.sent if path == "/api/sendText"] == ["Avec plaisir ! 😊"]


def test_no_reaction_to_a_plain_question_in_a_silent_group_or_from_a_blocked_member(waha):
    waha.emotions = Feels(Feeling(emotion="neutral", strength=0, reaction=""))
    asyncio.run(waha.handle(parse_message(message_event("When is the deadline?", chat_id=AWA), "Jeli")))
    waha.emotions = Feels(Feeling(emotion="sadness", strength=3, reaction="😢"))
    waha.silent_groups = {GROUP}
    asyncio.run(waha.handle(parse_message(message_event("Notre ami est décédé hier.", message_id="m2"), "Jeli")))
    assert waha.emotions.seen == []  # a silent group: not even felt

    class Guard:
        blocked = {"22370000000"}

        def check(self, message):
            return "blocked"

        def report(self, message, kind):
            pass

    waha.silent_groups, waha.guard = set(), Guard()
    asyncio.run(waha.handle(parse_message(message_event("Merci Jeli 😭❤️", chat_id=AWA, message_id="m3"), "Jeli")))
    assert reactions(waha) == []


def _feeling(emotion, strength, reaction="😂"):
    from app.answer.emotion import Feeling

    return Feeling(emotion=emotion, strength=strength, reaction=reaction, is_bad_news=False)


class Stickers:
    """The groups' own stickers, as the knowledge base keeps them."""

    def __init__(self, known=None):
        self.known = dict(known or {})
        self.remembered = []
        self.picked = []

    async def remember_sticker(self, file_url, emotion, chat_id=""):
        self.remembered.append((file_url, emotion, chat_id))
        self.known.setdefault(emotion, file_url)

    async def pick_sticker(self, emotion):
        self.picked.append(emotion)
        return self.known.get(emotion)


def _channel(store, **kw):
    from app.adapters.whatsapp_waha import Waha
    from app.config import Settings

    async def respond(message):
        return None

    waha = Waha(Settings(waha_url="http://waha.test", waha_api_key="k"), respond)
    waha.store = store
    for name, value in kw.items():
        setattr(waha, name, value)
    return waha


def test_jeli_answers_a_strong_feeling_with_one_of_the_groups_own_stickers():
    import asyncio

    from tests.test_whatsapp_waha import AWA, message_event
    from app.adapters.whatsapp_waha import parse_message

    sent = []
    store = Stickers({"humor": "http://waha.test/api/files/laugh.webp"})
    waha = _channel(store)
    waha.send_sticker = lambda chat, url='', reply_to=None, data=None: sent.append((chat, url, data))

    async def no_wait():
        return None

    waha.spacer.wait_turn = no_wait
    message = parse_message(message_event("😂😂", chat_id=AWA), "Jeli")
    asyncio.run(waha._answer_with_a_sticker(message, _feeling("humor", 4)))
    assert sent and sent[0][1] == "http://waha.test/api/files/laugh.webp"
    # A faint feeling, or one that calls for no sticker at all, gets nothing.
    sent.clear()
    asyncio.run(waha._answer_with_a_sticker(message, _feeling("humor", 1)))
    asyncio.run(waha._answer_with_a_sticker(message, _feeling("worry", 5)))
    assert sent == []
    # A feeling the groups have no sticker for yet: Jeli sends its own, as bytes (no hosting).
    asyncio.run(waha._answer_with_a_sticker(message, _feeling("joy", 5, reaction="🎉")))
    assert len(sent) == 1 and sent[0][1] == "" and sent[0][2][:4] == b"RIFF"


def test_jelis_own_stickers_say_the_same_thing_as_its_reactions():
    """Measured 23 Sep: Jeli had learned no sticker from the groups, so the feature could never
    fire. Its own pack is drawn from the very emojis it reacts with (scripts/make_stickers.py)."""
    from app.answer.emotion import REACTIONS
    from app.answer.stickers import for_reaction, known

    assert len(known()) >= 10, "the pack is missing: run python -m scripts.make_stickers"
    for emoji in REACTIONS:
        sticker = for_reaction(emoji)
        assert sticker and sticker[:4] == b"RIFF", emoji
    assert for_reaction("") is None and for_reaction("🦖") is None


def test_stickers_stay_rare_and_can_be_switched_off():
    import asyncio

    from app.adapters.whatsapp_waha import STICKERS_PER_HOUR
    from tests.test_whatsapp_waha import AWA, message_event
    from app.adapters.whatsapp_waha import parse_message

    sent = []
    store = Stickers({"humor": "http://waha.test/api/files/laugh.webp"})
    waha = _channel(store)
    waha.send_sticker = lambda chat, url='', reply_to=None, data=None: sent.append(url or data)

    async def no_wait():
        return None

    waha.spacer.wait_turn = no_wait
    message = parse_message(message_event("😂", chat_id=AWA), "Jeli")
    for _ in range(STICKERS_PER_HOUR + 3):
        asyncio.run(waha._answer_with_a_sticker(message, _feeling("humor", 4)))
    assert len(sent) == STICKERS_PER_HOUR  # a few an hour, then Jeli keeps quiet
    sent.clear()
    waha.enabled_stickers = False
    asyncio.run(waha._answer_with_a_sticker(message, _feeling("humor", 4)))
    assert sent == []


def test_jelis_stickers_move_and_fit_what_whatsapp_accepts():
    """A sticker that moves says more than a still one — and WhatsApp refuses one over 500 KB."""
    import io as _io
    from pathlib import Path

    from PIL import Image

    from app.answer.emotion import REACTIONS
    from app.answer.stickers import for_reaction

    for emoji in REACTIONS:
        data = for_reaction(emoji)
        assert data, emoji
        assert len(data) <= 500_000, (emoji, len(data))
        image = Image.open(_io.BytesIO(data))
        assert image.size == (512, 512), (emoji, image.size)
        assert getattr(image, "n_frames", 1) > 1, f"{emoji} is still"
    assert Path("assets/stickers").is_dir()


def test_jelis_own_sticker_is_sent_as_it_is_and_a_borrowed_one_is_converted():
    """WAHA converting an animated WebP would flatten it into a picture."""
    import asyncio

    sent = []
    waha = _channel(Stickers())
    async def remember(path, payload):
        sent.append((path, payload))
        return {}

    waha._post = remember
    asyncio.run(waha.send_sticker("c@g.us", data=b"RIFF-animated"))
    asyncio.run(waha.send_sticker("c@g.us", "http://waha.test/api/files/theirs.webp"))
    assert sent[0][1]["convert"] is False and "data" in sent[0][1]["file"]
    assert sent[1][1]["convert"] is True and "url" in sent[1][1]["file"]
