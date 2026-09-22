import asyncio
import json

import httpx
import pytest

from app.adapters import whatsapp_waha
from app.adapters.whatsapp_waha import Waha, parse_message
from app.answer.voice import asks_for_voice, sources, spoken, wav, without_voice_request
from app.config import Settings
from app.models import Reply
from tests.test_whatsapp_waha import AWA, BOT_LID, GROUP, message_event

VOICE_MEDIA = {"url": "http://localhost:3000/api/files/default/voice.oga", "mimetype": "audio/ogg; codecs=opus"}


def voice_event(chat_id=AWA, **payload):
    return message_event("", chat_id=chat_id, message_id="voice-1", hasMedia=True, media=VOICE_MEDIA, **payload)


def test_a_voice_note_is_a_message_to_listen_to():
    private = parse_message(voice_event(), "Jeli")
    assert private.addressed_to_bot and private.text == "" and private.voice_url == VOICE_MEDIA["url"]
    # In a group: for Jeli when it replies to Jeli; otherwise only if a conversation with Jeli is open.
    reply = parse_message(voice_event(GROUP, replyTo={"id": "A", "participant": f"{BOT_LID}@lid"}), "Jeli")
    assert reply.addressed_to_bot and reply.voice_url
    assert not parse_message(voice_event(GROUP), "Jeli").addressed_to_bot


def test_asking_for_a_voice_reply():
    assert asks_for_voice("Quelle est la date limite ? Réponds en vocal")
    assert asks_for_voice("send me a voice note about the prize") and asks_for_voice("reply by voice please")
    assert not asks_for_voice("Who is presenting in the Open Hour?")
    assert without_voice_request("Quelle est la date limite ? Réponds en vocal") == "Quelle est la date limite ?"
    assert without_voice_request("reply by voice: when is the deadline?") == "when is the deadline?"
    assert parse_message(message_event("When is the deadline? Answer by voice", chat_id=AWA), "Jeli").reply_by_voice


def test_a_voice_note_says_the_words_and_the_text_keeps_the_sources():
    reply = "@22370000000 The hackathon closes on *Thursday 24 September*.\n\n> *Diane* · METI cohort, Wed 16 Sep\n> Submissions close on 24 Sept\nhttps://youtu.be/x?t=10"
    assert spoken(reply) == "The hackathon closes on Thursday 24 September."
    assert sources(reply) == "> *Diane* · METI cohort, Wed 16 Sep\n> Submissions close on 24 Sept\nhttps://youtu.be/x?t=10"
    audio = wav(b"\0\0" * 24_000)
    assert audio.startswith(b"RIFF") and len(audio) == 48_044  # one second


class Voice:
    def __init__(self, heard="When is the hackathon deadline?", audio=wav(b"\0\0" * 2400)):
        self.heard, self.audio, self.said = heard, audio, []

    async def listen(self, audio, mimetype):
        assert audio == b"OggS voice" and mimetype.startswith("audio/ogg")
        return self.heard

    async def speak(self, text, language="en"):
        self.said.append(text)
        return self.audio


@pytest.fixture
def waha(monkeypatch):
    monkeypatch.setattr(whatsapp_waha, "reading_delay", lambda: 0)
    monkeypatch.setattr(whatsapp_waha, "typing_duration", lambda text: 0)
    monkeypatch.setattr(whatsapp_waha, "VOICE_RECORDING_SECONDS", 0)
    sent = []

    def handler(request):
        if request.method == "GET":
            assert request.url.path == "/api/files/default/voice.oga"  # asked to our WAHA, whatever host it named
            return httpx.Response(200, content=b"OggS voice")
        body = json.loads(request.content)
        sent.append((request.url.path, body))
        if request.url.path == "/api/sendVoice" and waha.voice_fails:
            return httpx.Response(500, json={"error": "cannot convert"})
        return httpx.Response(201, json={})

    settings = Settings(_env_file=None, waha_url="http://waha.test:3000", waha_api_key="key", waha_webhook_hmac_key="h",
                        whatsapp_min_send_interval_seconds=0)
    asked = []

    async def respond(message):
        asked.append(message.text)
        return Reply("The hackathon closes on Thursday 24 September.\n\n> *Diane* · METI cohort, Wed 16 Sep\n> Submissions close on 24 Sept")

    waha = Waha(settings, respond=respond)
    waha._http = httpx.AsyncClient(base_url=settings.waha_url, transport=httpx.MockTransport(handler))
    waha.paused, waha.voice_fails, waha.sent, waha.asked = False, False, sent, asked
    return waha


def test_asked_by_voice_jeli_answers_by_voice(waha):
    waha.voice = Voice()
    asyncio.run(waha.handle(parse_message(voice_event(), "Jeli")))
    assert waha.asked == ["When is the hackathon deadline?"]
    assert waha.voice.said == ["The hackathon closes on Thursday 24 September."]
    paths = [path for path, _ in waha.sent]
    assert paths == ["/api/sendSeen", "/api/startTyping", "/api/default/presence", "/api/stopTyping", "/api/sendVoice"]
    voice = waha.sent[4][1]
    assert voice["convert"] is True and voice["reply_to"] == "voice-1" and voice["file"]["mimetype"] == "audio/wav"


def test_when_the_voice_note_cannot_be_sent_the_answer_is_written(waha):
    waha.voice, waha.voice_fails = Voice(), True
    asyncio.run(waha.handle(parse_message(message_event("When is the deadline? Reply by voice", chat_id=AWA), "Jeli")))
    assert waha.asked == ["When is the deadline?"]
    [text] = [body for path, body in waha.sent if path == "/api/sendText"]
    assert text["text"].startswith("The hackathon closes on Thursday 24 September.")


def test_voice_notes_between_members_are_never_listened_to(waha):
    waha.voice = Voice()
    waha.in_conversation = lambda message: False
    asyncio.run(waha.handle(parse_message(voice_event(GROUP), "Jeli")))
    assert waha.sent == [] and waha.asked == []
    # In a conversation with Jeli, the member's voice note is listened to, then treated as a follow-up.
    waha.in_conversation = lambda message: True
    waha.follow_up = lambda message: message.text.endswith("?")
    asyncio.run(waha.handle(parse_message(voice_event(GROUP), "Jeli")))
    assert waha.asked == ["When is the hackathon deadline?"]


class _FakeModels:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, 0

    async def generate_content(self, **kwargs):
        self.calls += 1
        return await self.behaviour()


def _tts_voice(behaviours):
    from types import SimpleNamespace
    from app.answer.llm import LLM
    from app.answer.voice import Voice

    llm = LLM(["k"], ["m"])
    models = [_FakeModels(b) for b in behaviours]
    llm._clients = [SimpleNamespace(aio=SimpleNamespace(models=m)) for m in models]
    return Voice(llm), models


def test_a_slow_gemini_voice_is_given_up_quickly_and_then_skipped(monkeypatch):
    from app.answer import voice as voice_module

    async def hang():
        await asyncio.sleep(30)

    monkeypatch.setattr(voice_module, "GEMINI_TTS_TIMEOUT", 0.05)
    speaker, models = _tts_voice([hang] * 14)
    assert asyncio.run(speaker._speak_gemini("hello", "en")) is None
    # Two slow failures rest each model: 2 keys on each of the 2 models, not 14 keys.
    assert sum(m.calls for m in models) == 4
    assert asyncio.run(speaker._speak_gemini("hello", "en")) is None
    assert sum(m.calls for m in models) == 4  # both models resting: no call at all


def test_a_key_over_quota_rests_alone_and_the_next_key_speaks(monkeypatch):
    from types import SimpleNamespace
    from google.genai import errors

    async def over_quota():
        raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    async def speaks():
        pcm = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x00" * 100))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[pcm]))])

    speaker, models = _tts_voice([over_quota, over_quota, speaks])
    audio = asyncio.run(speaker._speak_gemini("hello", "en"))
    assert audio and audio[:4] == b"RIFF"
    assert [m.calls for m in models] == [1, 1, 1]
    asyncio.run(speaker._speak_gemini("hello", "en"))  # the two keys over quota are skipped
    assert [m.calls for m in models] == [1, 1, 2]


def test_ids_and_phone_numbers_are_never_read_aloud():
    said = spoken("Session le 24 dans 120363429618850959@g.us avec Diane, contact +251 34 567 8901, @23484567890 aussi.")
    assert "@g.us" not in said and "1203" not in said and "251" not in said and "23484" not in said
    assert "Diane" in said and "24" in said


class _ScriptLLM:
    _clients: list = []

    def __init__(self, said):
        self.said = said

    async def generate(self, prompt, schema, **kwargs):
        if isinstance(self.said, Exception):
            raise self.said
        return schema(text=self.said)


def _script(said, written="La date limite est le *jeudi 24 septembre* à 15h. Courage !"):
    from app.answer.voice import Voice

    return asyncio.run(Voice(_ScriptLLM(said)).script(written, "fr"))


def test_a_short_answer_is_said_again_in_spoken_language():
    said = "Ah, bonne nouvelle : c'est le jeudi 24 septembre, à 15h. Allez, courage !"
    assert _script(said) == said


def test_a_rewrite_that_loses_a_date_or_the_model_failing_reads_the_answer_as_it_is():
    from app.answer.llm import LLMUnavailable

    written = "La date limite est le *jeudi 24 septembre* à 15h. Courage !"
    assert _script("C'est bientôt, courage !") == written  # 24 and 15 lost
    assert _script(LLMUnavailable()) == written
    assert _script("") == written
    long = "Un long récapitulatif. " * 100  # longer than a digest the rewrite tells
    assert _script("court", long) == long  # digests are read as they are


class _LiveSession:
    def __init__(self, chunks, error=None):
        self.chunks, self.error, self.sent = chunks, error, []

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_client_content(self, turns, turn_complete):
        self.sent.append(turns.parts[0].text)

    async def receive(self):
        from types import SimpleNamespace

        for chunk in self.chunks:
            part = SimpleNamespace(inline_data=SimpleNamespace(data=chunk))
            yield SimpleNamespace(server_content=SimpleNamespace(model_turn=SimpleNamespace(parts=[part]), turn_complete=False))
        yield SimpleNamespace(server_content=SimpleNamespace(model_turn=None, turn_complete=True))


def _live_voice(sessions):
    """A Voice whose keys each open the given Live session (one per key)."""
    from types import SimpleNamespace

    from app.answer.llm import LLM
    from app.answer.voice import Voice

    llm = LLM(["k"], ["m"])
    configs = []

    def client(session):
        def connect(model, config):
            configs.append((model, config))
            return session

        return SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))

    llm._clients = [client(s) for s in sessions]
    return Voice(llm), configs


TEXT = "Bonne nouvelle, le hackathon se termine le jeudi 24 septembre, courage !"  # 73 characters, about 5 s
SECOND = b"\x00\x00" * 24_000


def test_the_native_gemini_voice_says_the_text_with_its_mood():
    from google.genai import errors

    over_quota = _LiveSession([], error=errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}))
    speaks = _LiveSession([SECOND] * 5)
    speaker, configs = _live_voice([over_quota, speaks])
    audio = asyncio.run(speaker._speak_live(TEXT, "joyful"))
    assert audio[:4] == b"RIFF" and len(audio) == 44 + 5 * len(SECOND)  # five seconds, as a WAV
    assert speaks.sent == [TEXT]  # the text itself: the instructions are the system's
    model, config = configs[-1]
    assert model == "gemini-3.1-flash-live-preview" and "big smile" in config.system_instruction
    assert "The text is in English: say it aloud in English" in config.system_instruction and "never translate" in config.system_instruction
    assert "word for word" in config.system_instruction and "never answer it" in config.system_instruction
    asyncio.run(speaker._speak_live(TEXT, "calm"))
    assert len(configs) == 3  # the key over quota is not asked again


def test_a_native_voice_note_that_does_not_fit_the_text_is_not_sent():
    too_long = _LiveSession([SECOND] * 40)  # 40 s for a 5-second text: it answered instead of reading
    speaker, configs = _live_voice([too_long])
    assert asyncio.run(speaker._speak_live(TEXT)) is None
    assert [model for model, _ in configs] == ["gemini-3.1-flash-live-preview", "gemini-2.5-flash-native-audio-latest"]
    assert asyncio.run(speaker._speak_live("x" * 2000)) is None  # longer than a minute: the faster TTS says it


def test_gemini_voices_come_first_and_the_fallback_voice_last(monkeypatch):
    from app.answer.voice import Voice

    speaker = Voice(_ScriptLLM("Super, c'est le jeudi 24 septembre !"))
    calls = []

    async def engine(name, audio):
        calls.append(name)
        return audio

    rewritten_in = []
    monkeypatch.setattr(speaker, "_rewrite", lambda text, language: rewritten_in.append(language) or _done((text, "joyful")))
    monkeypatch.setattr(speaker, "_speak_gemini", lambda text, language, mood: engine(("tts", language, mood), b"RIFF-tts"))
    monkeypatch.setattr(speaker, "_speak_live", lambda text, mood, language: engine(("live", language, mood), b"RIFF-live"))
    monkeypatch.setattr(speaker, "_speak_edge", lambda text, language, mood: engine(("edge", language, mood), b"mp3"))
    # Asked from the dashboard (no language given): the language is the one of what is said.
    assert asyncio.run(speaker.speak(TEXT)) == b"RIFF-tts"
    assert calls == [("tts", "fr", "joyful")]  # Gemini TTS speaks: nothing else is asked
    assert rewritten_in == ["fr"]  # said again in French, the answer's language, never translated
    monkeypatch.setattr(speaker, "_speak_gemini", lambda text, language, mood: engine(("tts", language, mood), None))
    assert asyncio.run(speaker.speak(TEXT, "fr")) == b"RIFF-live" and calls[-1] == ("live", "fr", "joyful")
    monkeypatch.setattr(speaker, "_speak_live", lambda text, mood, language: engine(("live", language, mood), None))
    assert asyncio.run(speaker.speak(TEXT, "fr")) == b"mp3" and calls[-1] == ("edge", "fr", "joyful")


async def _done(value):
    return value


def test_the_spoken_rewrite_gives_the_mood_and_edge_follows_it(monkeypatch):
    from app.answer import voice as voice_module
    from app.answer.voice import Voice

    class MoodLLM(_ScriptLLM):
        async def generate(self, prompt, schema, **kwargs):
            return schema(text=self.said, mood=self.mood)

    llm = MoodLLM("Oh, désolé, je n'ai pas trouvé la date du 24.")
    llm.mood = "Reassuring"
    assert asyncio.run(Voice(llm)._rewrite("Je n'ai pas trouvé la date du 24 dans les groupes.", "fr"))[1] == "reassuring"
    llm.mood = "furious"
    assert asyncio.run(Voice(llm)._rewrite("Je n'ai pas trouvé la date du 24 dans les groupes.", "fr"))[1] == "calm"
    made = {}

    class Communicate:
        def __init__(self, text, voice, rate, pitch):
            made.update(voice=voice, rate=rate, pitch=pitch)

        async def stream(self):
            yield {"type": "audio", "data": b"mp3"}

    monkeypatch.setattr(voice_module.edge_tts, "Communicate", Communicate)
    assert asyncio.run(Voice(llm)._speak_edge("Bonne nouvelle !", "fr", "joyful")) == b"mp3"
    assert made == {"voice": "fr-FR-VivienneMultilingualNeural", "rate": "+6%", "pitch": "+6Hz"}


def test_a_rewrite_that_gives_the_list_back_is_asked_again():
    from app.answer.voice import Voice

    class EchoOnce(_ScriptLLM):
        def __init__(self):
            self.calls = 0

        async def generate(self, prompt, schema, **kwargs):
            self.calls += 1
            assert prompt.startswith("Say this answer as a voice note, entirely in French, without a list:")
            if self.calls == 1:
                return schema(text=prompt.split("\n\n", 1)[1], mood="calm")  # the written answer, as it is
            return schema(text="Alors, mardi 22 septembre, tu finis le module 1, et jeudi 24 c'est la date limite.", mood="calm")

    listing = "Échéances\n• mardi 22 septembre — finir le module 1\n• jeudi 24 septembre — date limite"
    llm = EchoOnce()
    said = asyncio.run(Voice(llm).script(listing, "fr"))
    assert said.startswith("Alors, mardi 22 septembre") and llm.calls == 2


def test_a_list_is_summarised_as_a_person_would_tell_it():
    from app.answer.voice import SPEAK_SYSTEM, Voice

    listing = (
        "Échéances des 14 prochains jours\n"
        "• mar. 22 sept. — Wadhwani Ignite: complete Module 1 (appel, Charles Bolton, jeu. 17 sept.)\n"
        "• jeu. 24 sept. — Hackathon submission deadline (METI cohort, +234 ···84, lun. 21 sept.)"
    )
    # The model is told to leave out who announced what and when, and to keep what is coming.
    assert "leave out what a listener does not need" in SPEAK_SYSTEM and "keep the day, date and time" in SPEAK_SYSTEM
    told = "Alors, mardi 22 septembre tu finis le module 1 de Wadhwani Ignite, et jeudi 24 septembre c'est la soumission du hackathon."
    assert _script(told, listing) == told  # announcements' dates and numbers left out: a human summary
    assert _script("Mardi 22 septembre, puis le 25 septembre la soumission.", listing) == listing  # 25: a number it invented
    assert _script("Bientôt le module 1, puis le hackathon.", listing) == listing  # most of its dates lost


def test_the_voice_says_the_reply_in_the_language_the_model_chose(monkeypatch):
    from app.answer.voice import Voice

    speaker = Voice(_ScriptLLM(""))
    told = []

    async def rewrite(text, language):
        told.append(("rewrite", language))
        return text, "calm"

    async def tts(text, language, mood):
        told.append(("tts", language))
        return b"RIFF-tts"

    monkeypatch.setattr(speaker, "_rewrite", rewrite)
    monkeypatch.setattr(speaker, "_speak_gemini", tts)
    # An English answer full of French names: the word lists would guess French.
    mixed = "Your Wadhwani session is mardi 22 septembre at 3 PM, then the atelier with Diane."
    asyncio.run(speaker.speak(mixed, "en"))
    assert told == [("rewrite", "en"), ("tts", "en")]
    told.clear()
    asyncio.run(speaker.speak("La date limite est le jeudi 24 septembre, courage !"))  # none given: guessed
    assert told == [("rewrite", "fr"), ("tts", "fr")]


def test_the_responder_gives_its_reply_the_language_it_understood():
    from datetime import datetime, timezone

    from app.answer.responder import Responder
    from app.answer.understand import Understood
    from app.models import IncomingMessage

    class Understands:
        async def understand(self, text, language, turns=(), member=""):
            return Understood(kind="social", reply="Hello! How can I help?", standalone=text, queries=[], language="en")

    message = IncomingMessage("whatsapp", "g@g.us", "1", "Awa", "Bonjour Jeli, can you help me?", datetime(2026, 9, 22, tzinfo=timezone.utc), True, True)
    reply = asyncio.run(Responder(None, understander=Understands()).respond(message))
    assert reply == "Hello! How can I help?" and reply.language == "en"


class QuotaStore:
    def __init__(self):
        self.rows = {}

    async def voice_quota(self, day):
        return [{"key_id": k, "model": m, "used": used, "exhausted": ex} for (d, k, m), (used, ex) in self.rows.items() if d == day]

    async def count_voice(self, day, key_id, model, exhausted=False):
        used, ex = self.rows.get((day, key_id, model), (0, False))
        self.rows[(day, key_id, model)] = (used + (0 if exhausted else 1), ex or exhausted)


def test_the_voice_notes_left_today_follow_the_keys_and_survive_a_restart():
    from types import SimpleNamespace

    from google.genai import errors

    from app.answer.llm import LLM
    from app.answer.voice import GEMINI_TTS_MODELS, Voice

    calls = []

    def client(behaviour):
        async def generate_content(model, contents, config):
            calls.append(model)
            return await behaviour()

        return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))

    async def speaks():
        pcm = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x00" * 100))
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[pcm]))])

    async def over_quota():
        raise errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})

    llm = LLM(["key-a", "key-b", "key-c"], ["m"])
    llm._clients = [client(over_quota), client(speaks), client(speaks)]
    store = QuotaStore()
    voice = Voice(llm)
    voice.store = store
    quota = asyncio.run(voice.quota())
    assert (quota["total"], quota["remaining"], quota["keys"], quota["per_key"]) == (60, 60, 3, 20)  # 3 keys × 2 models × 10
    assert asyncio.run(voice._speak_gemini("Bonjour", "fr"))[:4] == b"RIFF"  # key a over quota, key b speaks
    assert asyncio.run(voice.quota())["remaining"] == 60 - 10 - 1  # a's quota on that model spent, one note on b
    calls.clear()
    voice._tts_key_resting.clear()  # even once the short rest is over, a spent key is not asked again today
    voice._tts_cursor = 0
    asyncio.run(voice._speak_gemini("Bonjour", "fr"))
    assert calls == [GEMINI_TTS_MODELS[0]]  # straight to key b
    # A restart: the count comes back from the database.
    again = Voice(llm)
    again.store = store
    assert asyncio.run(again.quota())["remaining"] == 60 - 10 - 2
    # A key Google refuses leaves the total.
    llm._disable_key(2)
    assert asyncio.run(again.quota())["total"] == 40
