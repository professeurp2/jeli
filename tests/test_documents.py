import asyncio
import io
from datetime import datetime, timezone

import pytest

from app.adapters.whatsapp_waha import parse_shared_document
from app.answer.documents import Block, Documents, FileChoice, Translated, read_pages
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
    with pytest.raises(ValueError, match="PDF, Word"):
        asyncio.run(documents.add("slides.pptx", b"x"))


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
