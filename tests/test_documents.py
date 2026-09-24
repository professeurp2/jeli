import asyncio
import io
from datetime import datetime, timezone

import pytest

from app.adapters.whatsapp_waha import parse_shared_document
from app.answer.documents import TYPES, Block, Documents, FileChoice, Translated, read_pages
from app.answer.language import TEXTS
from app.answer.pdf import build_pdf
from app.models import Attachment, Reply

T0 = datetime(2026, 9, 17, 20, 53, tzinfo=timezone.utc)
GUIDELINES = (
    "CHATBOT HACKATHON\n\nWe are launching a hackathon with a $5,000 cash prize.\n\n"
    "Build Phase: Friday 18 Sept to Thursday 24 Sept. Submit a working chatbot and its source code."
)


def pdf_of(text: str) -> bytes:
    return build_pdf("Hackathon guidelines", "A test document.", [("paragraph", line) for line in text.split("\n\n")])


def docx_of(text: str) -> bytes:
    from docx import Document

    document = Document()
    for paragraph in text.split("\n\n"):
        document.add_paragraph(paragraph)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def test_pdf_word_and_text_documents_are_read():
    assert "5,000 cash prize" in read_pages(".pdf", pdf_of(GUIDELINES))[0]
    assert "Build Phase" in " ".join(read_pages(".docx", docx_of(GUIDELINES)))
    assert read_pages(".txt", GUIDELINES.encode())[0].startswith("CHATBOT HACKATHON")


class Store:
    def __init__(self):
        self.documents_kept, self.contents, self.messages = {}, {}, []

    async def save_document(self, document, content):
        if document.id in self.documents_kept:
            return False
        self.documents_kept[document.id], self.contents[document.id] = document, content
        return True

    async def add_messages(self, messages):
        self.messages += messages
        return len(messages)

    async def list_documents(self):
        return [d for d in self.documents_kept.values() if d.translation_of is None]

    async def document_content(self, document_id):
        return self.contents.get(document_id)

    async def translation(self, document_id, language):
        return next((d for d in self.documents_kept.values() if d.translation_of == document_id and d.language == language), None)

    async def messages_of(self, chat_id):
        return [m for m in self.messages if m.chat_id == chat_id]


class LLM:
    def __init__(self, choice):
        self.choice, self.translations, self.requests = choice, 0, []

    async def generate(self, prompt, schema, **kwargs):
        if schema is FileChoice:
            return self.choice
        self.translations += 1
        self.requests.append(prompt)
        return Translated(
            blocks=[Block(kind="heading", text="HACKATHON DU CHATBOT"), Block(kind="paragraph", text="Prix de 5 000 $.")],
            title="Lignes directrices",
            note="Traduction automatique par Jeli ; l'original fait foi.",
        )


class Echo(LLM):
    """A model that returns the original instead of translating it (the light models did)."""

    async def generate(self, prompt, schema, **kwargs):
        if schema is FileChoice:
            return self.choice
        self.translations += 1
        text = prompt.split("\n\n", 1)[1].split("\n\nAlso return")[0]
        return Translated(blocks=[Block(kind="paragraph", text=part) for part in text.split("\n\n")])


def test_a_document_is_kept_and_learned_page_by_page():
    store = Store()
    documents = Documents(store, None)
    data = pdf_of(GUIDELINES)
    document, new = asyncio.run(documents.add("UniPods_Hackathon_Guidelines.pdf", data, shared_by="Diane", shared_at=T0))
    assert new and document.title == "UniPods Hackathon Guidelines" and document.pages == 1 and document.language == "en"
    assert document.id.startswith("document:unipods-hackathon-guidelines-")
    assert store.messages and all(m.chat_id == document.id and m.source == "document" for m in store.messages)
    assert asyncio.run(documents.add("again.pdf", data, title="UniPods Hackathon Guidelines"))[1] is False  # same file
    # A real slide deck is a ZIP, like .docx — so its bytes say nothing certain, and the name
    # decides. Jeli cannot read it either way, and says so.
    with pytest.raises(ValueError, match="PDF, Word"):
        asyncio.run(documents.add("slides.pptx", b"PK" + bytes([3, 4, 20, 0, 6, 0]) + bytes(200)))


def test_a_member_gets_the_document_they_ask_for():
    store = Store()
    asyncio.run(Documents(store, None).add("Guidelines.pdf", pdf_of(GUIDELINES), shared_by="Diane", shared_at=T0))
    reply = asyncio.run(Documents(store, LLM(FileChoice(document=1, translate_to=""))).reply("send me the guidelines", "en"))
    assert isinstance(reply, Reply) and reply.startswith("📄 Here's «Guidelines», shared by Diane on Thu 17 Sep.")
    assert reply.attachment.filename == "Guidelines.pdf" and reply.attachment.data.startswith(b"%PDF")
    assert asyncio.run(Documents(store, LLM(FileChoice(document=0, translate_to=""))).reply("what is the prize?", "en")) is None


def test_a_translation_comes_as_a_pdf_and_is_kept():
    store = Store()
    asyncio.run(Documents(store, None).add("Guidelines.pdf", pdf_of(GUIDELINES), shared_by="Diane", shared_at=T0))
    llm = LLM(FileChoice(document=1, translate_to="fr"))
    documents = Documents(store, llm)
    reply = asyncio.run(documents.reply("envoie-moi les guidelines en français", "fr"))
    assert reply.startswith("📄 Je traduis «Guidelines» en français") and reply.attachment is None
    translated = asyncio.run(reply.pending())
    assert isinstance(translated, Attachment) and translated.filename == "Guidelines (French).pdf"
    page = read_pages(".pdf", translated.data)[0]
    assert translated.data.startswith(b"%PDF") and "5 000" in page
    assert "Lignes directrices (français)" in page and "l'original fait foi" in page  # title and note translated too
    assert llm.requests[0].startswith("Translate into French:")
    again = asyncio.run(documents.reply("envoie-moi les guidelines en français", "fr"))
    assert again.attachment.filename == "Guidelines (French).pdf" and llm.translations == 1  # kept, not translated twice


def test_the_original_sent_back_is_never_passed_off_as_a_translation():
    store = Store()
    long_text = "\n\n".join([GUIDELINES, "Judging will be done by the whole group; more information on judging will follow shortly."] * 3)
    asyncio.run(Documents(store, None).add("Guidelines.pdf", pdf_of(long_text), shared_by="Diane", shared_at=T0))
    llm = Echo(FileChoice(document=1, translate_to="sw"))
    reply = asyncio.run(Documents(store, llm).reply("the guidelines in Swahili please", "en"))
    assert asyncio.run(reply.pending()) == TEXTS["en"]["file_translate_failed"].format(title="Guidelines")
    assert llm.translations == 2  # asked again once
    assert asyncio.run(store.translation(next(iter(store.documents_kept)), "sw")) is None  # nothing kept


def test_languages_the_pdf_cannot_write_are_declined():
    store = Store()
    asyncio.run(Documents(store, None).add("Guidelines.pdf", pdf_of(GUIDELINES), shared_at=T0))
    reply = asyncio.run(Documents(store, LLM(FileChoice(document=1, translate_to="ar"))).reply("in Arabic please", "en"))
    assert reply.startswith(TEXTS["en"]["file_language_unsupported"].split("{")[0])


def test_documents_shared_in_a_group_are_recognised():
    event = {
        "event": "message",
        "payload": {
            "id": "m1", "from": "120363@g.us", "fromMe": False, "hasMedia": True, "timestamp": T0.timestamp(),
            "body": "Guide for the demo videos", "_data": {"Info": {"PushName": "Diane"}},
            "media": {"url": "http://localhost:3000/api/files/default/abc.pdf", "mimetype": "application/pdf", "filename": "UniPods Video Demo Guide.pdf"},
        },
    }
    shared = parse_shared_document(event)
    assert shared["filename"] == "UniPods Video Demo Guide.pdf" and shared["author"] == "Diane" and shared["caption"] == "Guide for the demo videos"
    image = {**event, "payload": {**event["payload"], "media": {**event["payload"]["media"], "filename": "photo.jpg"}}}
    assert parse_shared_document(image) is None


TEAMS_TRANSCRIPT = """WEBVTT

1
00:00:03.120 --> 00:00:06.480
<v Diane Uwase>Bonjour à toutes et à tous, on commence.</v>

2
00:00:06.480 --> 00:00:11.200
<v Diane Uwase>La soumission du hackathon, c'est jeudi 24 septembre.</v>

3
00:00:11.900 --> 00:00:15.640
<v Romeo Tovonantenaina>Est-ce que les tests ferment vendredi ?</v>

4
00:00:15.640 --> 00:00:18.000
<v Diane Uwase>Oui, vendredi 25, dernier délai.</v>
"""


def test_a_teams_meeting_transcript_reads_as_a_conversation():
    """Teams and Meet export WebVTT: three quarters of it is timing and markup, and a model given
    it raw reads the clock instead of the words."""
    from app.answer.documents import extension, read_pages, read_vtt

    text = read_vtt(TEAMS_TRANSCRIPT)
    assert "Diane Uwase: Bonjour à toutes et à tous, on commence." in text
    assert "Romeo Tovonantenaina: Est-ce que les tests ferment vendredi ?" in text
    # The timing, the cue numbers and the markup are gone.
    assert "-->" not in text and "WEBVTT" not in text and "<v " not in text and "</v>" not in text
    # Someone speaking twice in a row is one turn, not two.
    assert text.count("Diane Uwase:") == 2
    # And it arrives as a document like any other.
    assert extension("Meeting Transcript.vtt") == ".vtt"
    assert extension("x", "text/vtt") == ".vtt"
    pages = read_pages(".vtt", TEAMS_TRANSCRIPT.encode())
    assert pages and "jeudi 24 septembre" in pages[0]


def test_a_transcript_shared_in_a_group_is_kept_like_any_document():
    from app.adapters.whatsapp_waha import DOCUMENT_TYPES

    assert ".vtt" in DOCUMENT_TYPES


# --- Something the team pastes ------------------------------------------------------------------


EMAIL = """From: Diane Mukasa <diane@unipod.org>
To: cohort-1@unipod.org
Date: Tue, 22 Sep 2026 09:14:00 +0200
Subject: Module 3 deadline moved to Friday

Dear all,

After yesterday's coaching session we are moving the Module 3 submission deadline
from Wednesday 23 September to Friday 25 September, 17:00 CAT. Teams that already
submitted do not need to do anything. The platform stays open until then.

Please share this with your teammates.

Best,
Diane

--
Diane Mukasa | UniPod METI AI Innovation Programme
This message and any attachments are confidential.

> On Mon, 21 Sep 2026, Stanley wrote:
> Is the Wednesday deadline still firm?
"""

BODY = """Dear all,

After yesterday's coaching session we are moving the Module 3 submission deadline
from Wednesday 23 September to Friday 25 September, 17:00 CAT. Teams that already
submitted do not need to do anything. The platform stays open until then.

Please share this with your teammates."""


class Reads:
    """A model that read the paste; `answer` is what it returns."""

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    async def generate(self, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return self.answer


class Keeps:
    """Just enough store for a document to be kept."""

    def __init__(self):
        self.saved = []

    async def documents(self, ids=None):
        return {}

    async def save_document(self, document, content):
        self.saved.append((document, content.decode("utf-8")))
        return True

    async def add_messages(self, messages):
        return len(messages)


def _paste(model, text=EMAIL, store=None):
    from app.answer.documents import Documents

    return asyncio.run(Documents(store or Keeps(), model).paste(text, pasted_by="Stanley"))


def test_an_email_the_team_pastes_becomes_a_document_with_who_sent_it_and_when():
    """Half of what this community decides never reaches WhatsApp: it arrives by email."""
    from app.answer.documents import Pasted

    model = Reads(Pasted(title="Module 3 deadline moved to Friday", sender="Diane Mukasa",
                         sent_at="2026-09-22T07:14:00Z", body=BODY))
    document, new = _paste(model)
    assert new and document.title == "Module 3 deadline moved to Friday"
    assert document.shared_by == "Diane Mukasa"  # not the teammate who pasted it
    assert document.shared_at.isoformat().startswith("2026-09-22T07:14")
    # The envelope is gone; the sentences members will ask about are not.
    assert document.filename.endswith(".txt")


def test_what_was_pasted_is_never_quietly_summarised():
    """A model asked to trim an envelope sometimes rewrites the whole thing. Members ask about the
    exact sentences, so a short answer is thrown away and the paste is kept whole."""
    from app.answer.documents import Pasted

    model = Reads(Pasted(title="Deadline moved", sender="Diane",
                         body="The Module 3 deadline moved to Friday."))
    store = Keeps()
    _paste(model, store=store)
    [(_, kept)] = store.saved
    assert "This message and any attachments are confidential" in kept  # the envelope came back
    assert "Teams that already" in kept  # and so did everything members might ask about


def test_a_paste_with_no_model_is_still_kept():
    """The team pastes something the minute it arrives; no engine is a reason to keep it as it is,
    not a reason to lose it."""
    store = Keeps()
    document, new = _paste(None, store=store)
    assert new and document.shared_by == "Stanley"  # nobody read who it was from
    assert document.title.startswith("From: Diane Mukasa")
    [(_, kept)] = store.saved
    assert "moving the Module 3 submission deadline" in kept


def test_a_date_read_wrong_is_dropped_rather_than_believed():
    from app.answer.documents import Pasted, _moment

    assert _moment("2026-09-22T07:14:00Z") is not None
    assert _moment("2035-01-01T00:00:00Z") is None  # years away: misread, not prophetic
    assert _moment("1998-01-01T00:00:00Z") is None  # before the community existed
    assert _moment("") is None and _moment("next Tuesday") is None


def test_too_little_or_too_much_is_refused_in_words_the_team_understands():
    from app.answer.documents import Documents, MAX_PASTED_CHARS

    documents = Documents(Keeps(), None)
    for text, said in (("ok", "not enough text"), ("x" * (MAX_PASTED_CHARS + 1), "too long")):
        try:
            asyncio.run(documents.paste(text))
            assert False, "accepted what it should refuse"
        except ValueError as error:
            assert said in str(error)


def test_the_team_can_reach_it_from_the_knowledge_page():
    import inspect

    from app.web import pages

    source = inspect.getsource(pages)
    assert '"/dashboard/knowledge/paste"' in source and "Paste an email or an announcement" in source
    assert "knowledge_paste" in source


# --- A file is what its bytes say, not what it was called ---------------------------------------


PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj"
DOCX = b"PK" + bytes([3, 4, 20, 0, 6, 0])  # a .docx, a .pptx and a .zip all start this way
TEXT = "METI UniPods Cohort 1 — Information Pack\n\nThe programme starts…".encode("utf-8")


def test_a_document_is_sent_under_the_name_its_bytes_deserve():
    """Measured 24 Sep: a member asked for the Information Pack and got a 4 KB "…Info pack.pdf"
    WhatsApp could not open. What had always been kept under that name was plain text."""
    from app.answer.documents import as_it_really_is

    assert as_it_really_is("Info pack.pdf", "application/pdf", TEXT) == ("Info pack.txt", "text/plain")
    # A real PDF keeps its name, and so does anything Jeli has no opinion about.
    assert as_it_really_is("Info pack.pdf", "application/pdf", PDF) == ("Info pack.pdf", "application/pdf")
    assert as_it_really_is("photo.png", "image/png", b"\x89PNG\r\n\x1a\n") == ("photo.png", "image/png")
    # Markdown and subtitles are text too: they keep the name that says which kind.
    assert as_it_really_is("notes.md", "text/markdown", b"# Notes")[0] == "notes.md"
    assert as_it_really_is("call.vtt", "text/vtt", b"WEBVTT\n\n00:00")[0] == "call.vtt"
    # A ZIP-based file (.docx, .pptx, .xlsx all start "PK") says nothing certain: the name stands.
    assert as_it_really_is("slides.pdf", "application/pdf", DOCX) == ("slides.pdf", "application/pdf")


def test_what_is_kept_is_named_for_what_it_is():
    """Correcting it on the way in, so the mistake is not made again for every future reader."""
    import inspect

    from app.answer.documents import Documents

    adding = inspect.getsource(Documents.add)
    assert "as_it_really_is(filename, mimetype, data)" in adding
    assert adding.index("as_it_really_is") < adding.index("ext = extension")


def test_an_old_row_with_the_wrong_name_is_still_sent_correctly():
    """The rows already stored wrong must reach members as something they can open."""
    import inspect

    from app.answer.documents import Documents

    assert "as_it_really_is(document.filename, document.mimetype, data)" in inspect.getsource(Documents.attachment)


def test_a_translation_is_a_real_pdf_whose_words_survive_it():
    """The other half of what a member asked: that the translated file opens, and says something."""
    import io as _io

    from pypdf import PdfReader

    from app.answer.pdf import build_pdf

    pdf = build_pdf(
        "METI UniPods Cohort 1 — Kifurushi cha Taarifa (Kiswahili)",
        "Traduit par Jeli",
        [("heading", "Tarehe za mwisho"),
         ("text", "Mpango unaanza tarehe 18 Septemba 2026. Échéances et accompagnement compris.")],
    )
    assert pdf.startswith(b"%PDF-") and b"%%EOF" in pdf[-400:]
    read_back = "\n".join((page.extract_text() or "") for page in PdfReader(_io.BytesIO(pdf)).pages)
    for word in ("Kifurushi", "Septemba", "Échéances", "accompagnement"):
        assert word in read_back, word
    # And it goes out as a PDF, because this time the bytes really are one.
    from app.answer.documents import as_it_really_is

    assert as_it_really_is("Info pack (Swahili).pdf", "application/pdf", pdf)[1] == "application/pdf"


def test_the_original_is_handed_over_not_a_text_copy_of_it():
    """Measured 24 Sep: a member asked for the Information Pack and got a 4 KB text copy, while
    Jeli held the 6-page PDF the community was actually given. Jeli sends what it was given."""
    from app.answer.documents import CHOOSE_SYSTEM, _kind
    from app.models import Document as Doc

    def entry(filename, mimetype):
        return Doc(id="x", title="t", filename=filename, mimetype=mimetype, size_bytes=0, pages=1,
                   language="en", shared_by="", shared_at=T0, chat_id="", translation_of=None)

    # The listing says what each entry is, in words: ".txt" at the end of a name is read past.
    assert _kind(entry("pack.pdf", "application/pdf")) == "the original PDF"
    assert _kind(entry("pack.txt", "text/plain")) == "a plain-text copy"
    assert _kind(entry("notes.docx", TYPES[".docx"])) == "the original Word file"
    assert _kind(entry("call.vtt", "text/vtt")) == "a meeting transcript"
    # And the rule for choosing between two of the same document is stated, not left to luck.
    assert "the original file the community was given" in CHOOSE_SYSTEM
    assert "even when the text copy's title matches" in CHOOSE_SYSTEM


def test_jeli_never_makes_up_a_document_it_was_not_given():
    """It may translate one into a PDF, because a member asked for that. It may not turn a text
    file it holds into a PDF and pass it off as the document: Jeli sends what it was given."""
    import inspect

    from app.answer import documents

    source = inspect.getsource(documents)
    assert "as_a_document" not in source
    sending = inspect.getsource(documents.Documents.attachment)
    assert "build_pdf" not in sending
