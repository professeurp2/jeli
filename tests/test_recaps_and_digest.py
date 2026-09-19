import asyncio
from datetime import datetime, time, timedelta, timezone

from app.answer.intents import is_recap_request
from app.answer.language import TEXTS
from app.answer.recaps import ActionItem, KeyMoment, Recaps, SessionRecap, format_recap, match_recordings
from app.jobs.daily_digest import next_run, post_daily_digests
from app.models import Recording, StoredMessage


def recording(id_, title, day, recap=None):
    return Recording(
        id=f"recording:{id_}", title=title, recorded_at=datetime(2026, 9, day, 13, 0, tzinfo=timezone.utc),
        method="gemini", source_url=f"https://youtu.be/{id_}0000000", duration_seconds=6000, recap=recap,
    )


WELCOME = recording("welcome", "Wadhwani Ignite — Welcome session and Module 0", 10)
MODULE1 = recording("module1", "Wadhwani Ignite — Module 1 class session", 15)
COACHING = recording("coaching", "Wadhwani Ignite — Module 1 Problem Statement coaching", 17)
ALL = [WELCOME, MODULE1, COACHING]

RECAP = SessionRecap(
    summary=["Module 1 has four lessons and three activities."],
    decisions=["Team size on the platform stays at six."],
    action_items=[ActionItem(action="Complete the three Module 1 activities", owner="all teams", due="Mon 21 Sep")],
    key_moments=[KeyMoment(time="05:29", topic="Who fills in the dashboard"), KeyMoment(time="not a time", topic="x")],
).model_dump()


def test_recap_requests():
    assert is_recap_request("/recap")
    assert is_recap_request("Summary of the Module 1 session?")
    assert is_recap_request("De quoi a-t-on parlé pendant le coaching ?")
    assert is_recap_request("What was decided in the welcome call?")
    assert not is_recap_request("When is the bootcamp?")
    assert not is_recap_request("In the Module 1 class, do all team members need to complete the course?")


def test_sessions_are_matched_by_distinctive_words_or_date():
    assert match_recordings("summary of the coaching", ALL) == [COACHING]
    assert match_recordings("recap of the welcome session", ALL) == [WELCOME]
    assert match_recordings("résumé de la session du 15 septembre", ALL) == [MODULE1]
    assert match_recordings("Module 1 class recap", ALL) == [MODULE1]
    # "Module 1" alone fits two sessions: both are returned, so Jeli can ask which one.
    assert match_recordings("summary of the Module 1 session", ALL) == [MODULE1, COACHING]
    assert match_recordings("recap of the bootcamp", ALL) == []


def test_recap_format_links_key_moments_to_the_video():
    text = format_recap(MODULE1, RECAP, "en")
    assert text.startswith("🎥 Wadhwani Ignite — Module 1 class session · 15 Sep 2026 · 1:40:00\nhttps://youtu.be/module10000000")
    assert "📝 Summary\n• Module 1 has four lessons and three activities." in text
    assert "📋 To do\n• Complete the three Module 1 activities — all teams · Mon 21 Sep" in text
    assert "⏱️ Key moments\n• 05:29 Who fills in the dashboard\n  https://youtu.be/module10000000?t=329" in text
    assert "not a time" not in text


class FakeStore:
    def __init__(self, recordings):
        self.recordings = recordings
        self.saved = []

    async def all_recordings(self):
        return self.recordings

    async def messages_of(self, chat_id):
        return [StoredMessage("s1", chat_id, "recording", "Charles", MODULE1.recorded_at + timedelta(minutes=5), "Hello")]

    async def save_recap(self, recording_id, language, data):
        self.saved.append((recording_id, language))


class FakeLLM:
    def __init__(self):
        self.prompts = []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return SessionRecap.model_validate(RECAP)


def test_stored_recaps_are_reused_and_missing_languages_generated_once():
    module1 = recording("module1", MODULE1.title, 15, recap={"en": RECAP})
    store, llm = FakeStore([WELCOME, module1, COACHING]), FakeLLM()
    recaps = Recaps(store, llm)
    english = asyncio.run(recaps.reply("Module 1 class recap", "en"))
    assert english.startswith("🎥 Wadhwani Ignite — Module 1 class session") and llm.prompts == []
    french = asyncio.run(recaps.reply("résumé du cours module 1 class", "fr"))
    assert "📝 Résumé" in french
    assert store.saved == [(module1.id, "fr")]
    assert "[05:00] Charles: Hello" in llm.prompts[0] and llm.prompts[0].endswith("Write every item in French.")


def test_ambiguous_or_bare_recap_requests_list_the_sessions():
    recaps = Recaps(FakeStore(ALL), FakeLLM())
    listed = asyncio.run(recaps.reply("summary of the Module 1 session", "en"))
    assert listed == TEXTS["en"]["recap_choose"] + "\n2. Wadhwani Ignite — Module 1 class session (15 Sep 2026)\n3. Wadhwani Ignite — Module 1 Problem Statement coaching (17 Sep 2026)"
    everything = asyncio.run(recaps.reply("/recap", "en"))
    assert everything.count("\n") == 3
    assert asyncio.run(recaps.reply("/recap 3", "en")).startswith("🎥 Wadhwani Ignite — Module 1 Problem Statement coaching")
    assert asyncio.run(recaps.reply("summary of the bootcamp session", "en")) is None


def test_next_daily_run():
    at = time(17, 0)
    assert next_run(datetime(2026, 9, 19, 9, 0, tzinfo=timezone.utc), at) == datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)
    assert next_run(datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc), at) == datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)


class DigestStore:
    def __init__(self):
        self.claimed = set()

    async def claim_daily_run(self, job, day):
        if (job, day) in self.claimed:
            return False
        self.claimed.add((job, day))
        return True


class DigestCatchup:
    def __init__(self, news):
        self.news = news

    async def summarize(self, since, language, chat_ids=None, quiet_if_empty=False):
        return f"digest of {chat_ids[0]}" if chat_ids[0] in self.news else None


def test_daily_digest_is_posted_once_per_group_and_only_with_news():
    store, posted = DigestStore(), []

    async def post(chat_id, text):
        posted.append((chat_id, text))
        return True

    now = datetime(2026, 9, 19, 17, 0, tzinfo=timezone.utc)
    catchup = DigestCatchup(news={"a@g.us"})
    assert asyncio.run(post_daily_digests(store, catchup, post, ["a@g.us", "b@g.us"], "en", now)) == 1
    assert posted == [("a@g.us", "digest of a@g.us")]
    # Same day again (e.g. after a restart): nothing is sent twice.
    assert asyncio.run(post_daily_digests(store, catchup, post, ["a@g.us", "b@g.us"], "en", now)) == 0
    assert len(posted) == 1
