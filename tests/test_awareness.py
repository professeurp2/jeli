import asyncio
import io
import zipfile
from datetime import datetime, timedelta, timezone

from app.answer.awareness import Awareness, Explanation, progress_words
from app.answer.language import TEXTS
from app.answer.responder import _outcome
from app.ingest.sessions import RecordingShare, SessionImport, Sessions
from app.ingest.whatsapp_export import attachments, export_documents, who_shared
from app.models import IncomingMessage, Recording, Reply

NOW = datetime.now(timezone.utc)


class Store:
    def __init__(self):
        self.recordings_saved = {}

    async def knowledge_overview(self):
        return {"chats": [{"chat_id": "meti", "messages": 10, "last_message": datetime(2026, 9, 18, 20, 3, tzinfo=timezone.utc), "live": 0}],
                "recordings": [], "chunks": 0, "pending": 0, "deadlines": 0}

    async def all_recordings(self):
        return list(self.recordings_saved.values())

    async def recordings(self, ids):
        return {i: self.recordings_saved[i] for i in ids if i in self.recordings_saved}

    async def save_recording(self, recording):
        self.recordings_saved[recording.id] = recording

    async def deadlines_between(self, start, end, include_dismissed=False):
        return []

    async def list_documents(self):
        return []

    async def mentioned_documents(self, limit=60):
        return [{"author": "Diane", "sent_at": NOW, "chat_id": "meti", "text": "<document omis> UniPods Video Demo Guide.pdf"}]


class LLM:
    def __init__(self, result):
        self.result, self.prompts = result, []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return self.result


class FakeSessions:
    def __init__(self, jobs):
        self.jobs = jobs

    def in_progress(self):
        return self.jobs


def test_a_question_about_a_session_being_transcribed_is_told_so():
    job = SessionImport("https://youtu.be/abcdefgh", "Wadhwani Ignite — Module 2 class", NOW, "Diane", state="transcribing",
                        progress="0:00–30:00: 42 segments")
    awareness = Awareness(Store(), None, FakeSessions([job]))
    reply = asyncio.run(awareness.explain("What was said in the Module 2 class?", "en"))
    assert reply.startswith("⏳ The recording of «Wadhwani Ignite — Module 2 class» (shared by Diane,")
    assert "the first 30 minutes are done" in reply
    assert progress_words("", "fr") == "elle vient de commencer"


def test_otherwise_jeli_explains_from_what_it_knows_of_its_state():
    store = Store()
    store.recordings_saved["recording:x"] = Recording("recording:x", "Open Hour with Diane", NOW, "link", source_url="https://teams.example/rec")
    llm = LLM(Explanation(reply="The groups haven't covered the Open Hour yet; its recording is a Teams link: https://teams.example/rec"))
    reply = asyncio.run(Awareness(store, llm, FakeSessions([])).explain("What was said in the Open Hour?", "en", quotes="> *Diane* · METI"))
    assert reply == "The groups haven't covered the Open Hour yet; its recording is a Teams link: https://teams.example/rec\n\n> *Diane* · METI"
    state = llm.prompts[0]
    assert "memory of the groups goes up to Friday 18 September 2026, 20:03 UTC; it is not connected to the groups yet" in state
    assert "Recording shared as a link Jeli cannot watch: Open Hour with Diane" in state
    assert "UniPods Video Demo Guide.pdf (shared by Diane" in state  # a document it knows of but does not have


def test_an_explanation_counts_as_a_question_jeli_could_not_answer():
    assert _outcome("question", Reply("The groups haven't covered it yet.", unanswered=True), "en") == "dont_know"
    assert _outcome("question", "It closes on Thursday.", "en") == "answered"


def message(text):
    return IncomingMessage("whatsapp", "g@g.us", "1", "Diane", text, datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc), False, False,
                           author_id="263774094822@c.us")


def test_an_organisers_recording_is_recognised_and_added():
    store = Store()
    started = []
    youtube = LLM(RecordingShare(is_recording=True, title="Module 2 class", session_day="2026-09-18", url="https://youtu.be/abcdefgh1"))
    sessions = Sessions(store, transcription=None, recaps=None, reader=youtube)
    sessions.start = lambda url, title, recorded_at, by: started.append((url, title, recorded_at.date().isoformat(), by)) or "job"
    asyncio.run(sessions.from_organiser(message("Here is the recording of yesterday's Module 2 class: https://youtu.be/abcdefgh1")))
    assert started == [("https://youtu.be/abcdefgh1", "Module 2 class", "2026-09-18", "Diane")]
    # A Teams recording cannot be watched: kept as a link, so Jeli can say where it is.
    teams = LLM(RecordingShare(is_recording=True, title="Open Hour", session_day="", url="https://teams.microsoft.com/rec/1"))
    job = asyncio.run(Sessions(store, None, None, reader=teams).from_organiser(message("Open Hour recording: https://teams.microsoft.com/rec/1")))
    assert job.state == "link"
    [kept] = [r for r in store.recordings_saved.values() if r.method == "link"]
    assert kept.title == "Open Hour" and kept.source_url == "https://teams.microsoft.com/rec/1"
    # Not a recording: nothing happens.
    nothing = LLM(RecordingShare(is_recording=False, title="", session_day="", url=""))
    assert asyncio.run(Sessions(store, None, None, reader=nothing).from_organiser(message("Register here: https://forms.gle/x"))) is None


def test_an_export_with_media_gives_its_documents_and_who_shared_them():
    chat = (
        "[16/09/2026, 10:12:00] Diane: Here are the rules\n"
        "[16/09/2026, 10:12:30] Diane: ‎<attached: 00000012-UniPods Hackathon Guidelines.pdf>\n"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("_chat.txt", chat)
        archive.writestr("00000012-UniPods Hackathon Guidelines.pdf", b"%PDF-1.4 test")
        archive.writestr("00000013-PHOTO.jpg", b"jpg")
    [(name, data)] = export_documents("WhatsApp Chat.zip", out.getvalue())
    assert name == "00000012-UniPods Hackathon Guidelines.pdf" and data.startswith(b"%PDF")
    by = who_shared(name, attachments(chat))
    assert by.author == "Diane" and by.sent_at.date().isoformat() == "2026-09-16"


def test_polls_are_remembered_with_their_votes():
    from app.adapters.whatsapp_waha import parse_message
    from app.answer.citations import with_tally

    event = {
        "event": "message", "session": "default", "me": {"id": "22380000000@c.us"},
        "payload": {
            "id": "poll-1", "from": "120363@g.us", "participant": "250783188655@c.us", "fromMe": False, "body": "",
            "timestamp": 1758300000, "_data": {"Info": {"PushName": "Diane"}, "Message": {"pollCreationMessageV3": {
                "name": "Which day for the demo?", "options": [{"optionName": "Tuesday"}, {"optionName": "Wednesday"}]}}},
        },
    }
    message = parse_message(event, "Jeli")
    assert message.text == "📊 Poll: Which day for the demo?\nOptions: Tuesday · Wednesday"
    assert with_tally(message.text, {"Wednesday": 7, "Tuesday": 3}).endswith("Votes so far: Wednesday 7 · Tuesday 3")
    assert with_tally(message.text, None).endswith("No votes yet.") and with_tally("Hello", None) == "Hello"
    reply = {**event, "payload": {**event["payload"], "body": "I vote Tuesday", "_data": {"Info": {"PushName": "Awa"},
             "quotedMessage": {"pollCreationMessageV3": {"name": "Which day?", "options": [{"optionName": "Tuesday"}]}}}}}
    assert parse_message(reply, "Jeli").text == "I vote Tuesday"  # a reply to a poll is not a poll


def test_organisers_are_known_by_name_and_number():
    from app.answer.citations import display_person, ignored_keys, people_names

    entries = ["Diane +250 783 188 655", "Gift NTULI +263 77 409 4822", "Munira"]
    assert ignored_keys(entries) == {"diane", "250783188655", "gift ntuli", "263774094822", "munira"}
    assert people_names(entries) == {"250783188655": "Diane", "263774094822": "Gift NTULI"}
    assert display_person("Diane +250 783 188 655") == "Diane (+250 ···55)"
