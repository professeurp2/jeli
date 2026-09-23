import asyncio
from datetime import datetime, timedelta, timezone

from app.answer.catchup import Catchup, Digest
from app.answer.intents import catchup_since, looks_like_question, parse_since
from app.answer.language import TEXTS
from app.answer.llm import LLMUnavailable
from app.models import Recording, StoredMessage

# Saturday 19 September 2026, 10:00 UTC
NOW = datetime(2026, 9, 19, 10, 0, tzinfo=timezone.utc)
MONDAY = datetime(2026, 9, 14, tzinfo=timezone.utc)


def test_periods():
    assert parse_since("what did I miss since Monday?", NOW) == MONDAY
    assert parse_since("qu'est-ce que j'ai raté depuis lundi", NOW) == MONDAY
    assert parse_since("since Saturday", NOW) == datetime(2026, 9, 19, tzinfo=timezone.utc)
    assert parse_since("/catchup 3 days", NOW) == NOW - timedelta(days=3)
    assert parse_since("/catchup 6h", NOW) == NOW - timedelta(hours=6)
    assert parse_since("recap of this week", NOW) == MONDAY
    assert parse_since("résumé de la semaine dernière", NOW) == MONDAY - timedelta(days=7)
    assert parse_since("what's new today", NOW) == datetime(2026, 9, 19, tzinfo=timezone.utc)
    assert parse_since("hier", NOW) == datetime(2026, 9, 18, tzinfo=timezone.utc)
    assert parse_since("/catchup", NOW) == NOW - timedelta(hours=24)


def test_catchup_requests_are_told_apart_from_questions_about_a_session():
    assert catchup_since("/catchup", NOW) is not None
    assert catchup_since("What did I miss?", NOW) is not None
    assert catchup_since("J'ai raté quoi cette semaine ?", NOW) is not None
    assert catchup_since("Give me a recap of this week", NOW) is not None
    assert catchup_since("Summarise the Module 1 session", NOW) is None
    assert catchup_since("When is the bootcamp?", NOW) is None


def test_questions_worth_checking():
    assert looks_like_question("When is the deadline for the hackathon?")
    assert looks_like_question("Quand est la date limite")
    assert not looks_like_question("ok?")
    assert not looks_like_question("Thanks everyone, see you tomorrow")
    assert not looks_like_question("https://teams.microsoft.com/l/meetup-join/19%3ameeting?x=1")


MESSAGES = [
    StoredMessage("m1", "meti", "whatsapp_export", "Awa Traoré", MONDAY + timedelta(hours=9), "Submissions close Thursday 24 Sep."),
    StoredMessage("m2", "meti", "whatsapp_export", "+234 818 554 6555", MONDAY + timedelta(hours=10), "Where is the recording?"),
    StoredMessage("m3", "meti", "whatsapp_export", "OtherBot", MONDAY + timedelta(hours=11), "I am a bot."),
]
RECORDING = Recording("recording:m1", "Module 1 class session", MONDAY + timedelta(days=1), "gemini", "https://youtu.be/6q4uPBO_sDc")


class FakeStore:
    def __init__(self, messages=MESSAGES, recordings=(RECORDING,)):
        self.messages, self.recordings = list(messages), list(recordings)

    async def messages_since(self, since, chat_ids=None):
        return [m for m in self.messages if m.sent_at >= since and (chat_ids is None or m.chat_id in chat_ids)]

    async def recordings_since(self, since):
        return [r for r in self.recordings if r.recorded_at >= since]


class FakeLLM:
    def __init__(self, digest=None, error=None):
        self.digest, self.error, self.prompts = digest, error, []

    async def generate(self, prompt, schema, system=None, timeout=None):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.digest


DIGEST = Digest(
    items=[
        "*Hackathon submissions* (Thu 24 Sep): submit chatbot + code + install notes.",
        "*Recording unavailable* — Module 1 class session link not yet shared.",
    ],
)


def test_digest_sections_and_recordings():
    llm = FakeLLM(DIGEST)
    digest = asyncio.run(Catchup(FakeStore(), llm, ignored_authors=["OtherBot"], chat_labels={"meti": "METI cohort"}).summarize(MONDAY, "en"))
    assert digest.startswith("🗓️ Catch-up since Mon 14 Sep (2 messages)")
    assert "• *Hackathon submissions*" in digest
    assert "• *Recording unavailable*" in digest
    assert "📣" not in digest and "✅" not in digest and "❓" not in digest  # no section headers
    assert "🎥 Recorded sessions\n• Module 1 class session (Tue 15 Sep)\n  https://youtu.be/6q4uPBO_sDc" in digest
    [prompt] = llm.prompts
    assert "METI cohort · Awa Traoré: Submissions close" in prompt
    assert "I am a bot" not in prompt and "818 554 6555" not in prompt
    assert prompt.startswith("Write in English. Today is ") and prompt.endswith("Write the intro and every item in English.")


def test_the_same_catchup_is_summarised_once():
    llm = FakeLLM(DIGEST)
    catchup = Catchup(FakeStore(), llm)
    first = asyncio.run(catchup.summarize(MONDAY, "en"))
    assert asyncio.run(catchup.summarize(MONDAY, "en")) == first
    assert len(llm.prompts) == 1
    asyncio.run(catchup.summarize(MONDAY, "fr"))
    assert len(llm.prompts) == 2


def test_quiet_periods_and_unavailable_models():
    quiet = Catchup(FakeStore(messages=[], recordings=[]), FakeLLM(DIGEST))
    assert asyncio.run(quiet.summarize(NOW, "fr")) == TEXTS["fr"]["catchup_nothing"].format(since="sam. 19 sept.")
    down = Catchup(FakeStore(), FakeLLM(error=LLMUnavailable()))
    assert "can't summarise them right now" in asyncio.run(down.summarize(MONDAY, "en"))


def test_the_digest_opens_with_the_gist_in_jelis_own_words():
    coming = "⏰ Coming up\n• Thu 24 Sep — Hackathon: submit the chatbot"

    class Deadlines:
        async def coming_up_section(self, language):
            return coming

    said = Digest(
        items=["*Hackathon submissions* (Thu 24 Sep): chatbot + code + install notes."],
        intro="A fairly quiet start of the week: Awa reminded everyone about the submission. The big one is Thursday — the hackathon closes.",
    )
    llm = FakeLLM(said)
    digest = asyncio.run(Catchup(FakeStore(), llm, ignored_authors=["OtherBot"], deadlines=Deadlines()).summarize(MONDAY, "en"))
    intro, rest = digest.split("\n\n", 1)
    assert intro == said.intro
    assert rest.startswith("🗓️ Catch-up since Mon 14 Sep (2 messages)")
    # What is coming is folded into the model's own items: no second list repeating them, no sources.
    assert "⏰ Coming up" not in digest
    # The model sees what is coming up and today's date (for "tomorrow" to be true).
    assert "Coming up (fold what matters into your items, without the sources in brackets):\n" + coming in llm.prompts[0]
    # An intro too long to be one: the digest as before.
    long = Digest(items=said.items, intro="blah " * 200)
    assert asyncio.run(Catchup(FakeStore(), FakeLLM(long)).summarize(MONDAY, "en")).startswith("🗓️ Catch-up since")


def test_a_mention_is_written_as_a_person_everywhere_at_once():
    """Seen 23 Sep in a catch-up: "*Presentation slides* @216324735279308 asked if the slides…".
    That id is what WhatsApp stores; the app shows a name, and so must Jeli."""
    from app.answer.citations import named_mentions

    names = {"216324735279308": "Diane"}
    said = "@216324735279308 asked if the slides will be shared"
    assert named_mentions(said, names) == "@Diane asked if the slides will be shared"
    # Someone Jeli cannot name is "someone" — true, and readable.
    assert named_mentions(said, {}) == "someone asked if the slides will be shared"
    # An email address or a price is not a mention.
    assert named_mentions("write to a@b.com about the 12345 francs", names) == "write to a@b.com about the 12345 francs"


def test_the_repair_happens_where_every_message_leaves_the_memory():
    """Not in the catch-up: in the one place they all pass through, so the recaps, the answers,
    the deadline finder and the indexed passages are mended by the same line."""
    from datetime import datetime, timezone

    from app.kb.store import Store

    store = object.__new__(Store)
    store._names = {"216324735279308": "Diane"}
    row = {
        "id": "m1", "chat_id": "c", "source": "whatsapp_live", "author": "Awa", "author_id": "1@lid",
        "sent_at": datetime.now(timezone.utc), "text": "@216324735279308 will share the slides",
    }
    assert store._readable(row).text == "@Diane will share the slides"
    # A message without a mention is returned untouched, and costs nothing.
    plain = {**row, "text": "see you tomorrow"}
    assert store._readable(plain).text == "see you tomorrow"
