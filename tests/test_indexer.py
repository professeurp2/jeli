import asyncio
from datetime import datetime, timedelta, timezone

from app.ingest.live import LiveIngestor, to_stored
from app.kb.embeddings import MODEL
from app.kb.indexer import index_pending
from app.kb.search import keyword_query
from app.models import IncomingMessage, StoredMessage

NOW = datetime.now(timezone.utc)


def stored(chat_id, minutes_ago, text="hello"):
    return StoredMessage(
        id=f"{chat_id}-{minutes_ago}", chat_id=chat_id, source="whatsapp_live", author="Awa",
        sent_at=NOW - timedelta(minutes=minutes_ago), text=text,
    )


class FakeStore:
    def __init__(self, messages=()):
        self.messages = list(messages)
        self.saved = []
        self.spares = []

    async def add_messages(self, messages):
        self.messages.extend(messages)
        return len(messages)

    async def pending_chats(self):
        return sorted({m.chat_id for m in self.messages if m.id not in self.indexed()})

    async def pending_messages(self, chat_id):
        return [m for m in self.messages if m.chat_id == chat_id and m.id not in self.indexed()]

    async def save_chunk(self, chunk, embedding, model, backup=None):
        self.saved.append((chunk, embedding, model))
        self.spares.append(backup)
        return len(self.saved)

    def indexed(self):
        return {id_ for chunk, _, _ in self.saved for id_ in chunk.message_ids}


class FakeEmbedder:
    async def embed_documents(self, texts):
        return [[0.1] * 768 for _ in texts]


def test_all_pending_messages_are_indexed_per_chat():
    store = FakeStore([stored("a", 300), stored("a", 299), stored("b", 200), stored("a", 100)])
    created = asyncio.run(index_pending(store, FakeEmbedder()))
    assert created == 3  # chat a: two conversations (300-299, then 100); chat b: one
    assert {chunk.chat_id for chunk, _, _ in store.saved} == {"a", "b"}
    assert all(model == MODEL for _, _, model in store.saved)
    assert asyncio.run(index_pending(store, FakeEmbedder())) == 0


def test_a_conversation_still_going_on_waits():
    store = FakeStore([stored("a", 120), stored("a", 5), stored("a", 1)])
    created = asyncio.run(index_pending(store, FakeEmbedder(), settle=timedelta(minutes=30)))
    assert created == 1
    assert store.saved[0][0].message_ids == ("a-120",)


def incoming(is_private=False, text="The deadline is Friday"):
    return IncomingMessage(
        platform="whatsapp", chat_id="g@g.us", message_id="false_g@g.us_AAA", author="Awa",
        text=text, sent_at=NOW, is_private=is_private, addressed_to_bot=False, author_id="223@c.us",
    )


def test_group_messages_are_remembered_but_direct_messages_are_not():
    store = FakeStore()
    ingestor = LiveIngestor(store)
    asyncio.run(ingestor.ingest(incoming()))
    asyncio.run(ingestor.ingest(incoming(is_private=True)))
    [message] = store.messages
    assert message == to_stored(incoming())
    assert message.id == "whatsapp:false_g@g.us_AAA" and message.source == "whatsapp_live"


def test_the_semantically_closest_chunks_are_never_crowded_out():
    from app.kb.search import select
    from app.kb.store import SearchHit

    def hit(chunk_id, similarity):
        return SearchHit(chunk_id, "c", NOW, NOW, [], [], "", 0.0, similarity)

    # Fused order (as the store returns it): keyword matches first, the best semantic match last.
    fused = [hit(1, 0.67), hit(2, 0.68), hit(3, 0.66), hit(4, 0.69), hit(5, 0.65), hit(6, 0.66), hit(7, 0.72)]
    chosen = select(fused, limit=4)
    assert [h.chunk_id for h in chosen] == [1, 2, 4, 7]  # 7 and 4 are the two closest; fused order kept


def test_keyword_query_keeps_meaningful_words():
    assert keyword_query("What was decided about the bootcamp dates?") == "decided | bootcamp | dates"
    assert keyword_query("Quand est le bootcamp ?") == "bootcamp"
    assert keyword_query("the and?") is None


def test_a_new_message_becomes_answerable_within_half_a_minute():
    """Measured 24 September: a member asked about something just said and Jeli did not know it
    yet. The wait is two things added together — how long a conversation must be quiet before it
    is learned, and how often the memory job looks — and it came to ninety seconds."""
    from datetime import timedelta

    from app.config import Settings
    from app.control.setup import SETTLE

    interval = Settings.model_fields["index_interval_seconds"].default
    worst_case = SETTLE + timedelta(seconds=interval)
    assert worst_case <= timedelta(seconds=30), f"a new message waits up to {worst_case}"
    # And not so short that two messages of the same thought are cut apart.
    assert SETTLE >= timedelta(seconds=15)


def test_a_round_with_nothing_to_learn_costs_no_model_call():
    """Polling often is only free while an empty round stays empty: the moment it embeds something
    unconditionally, ten seconds becomes a quota bill."""
    import inspect

    from app.kb.indexer import catch_up_gemini, index_pending

    early = inspect.getsource(catch_up_gemini)
    assert "if not waiting:" in early and "return 0" in early  # nothing waiting, nothing embedded
    # And the chunk loop only embeds what it has: no chunks, no call.
    assert "for start in range(0, len(chunks), BATCH_SIZE)" in inspect.getsource(index_pending)


def test_messages_held_back_are_not_lost():
    """The last chunk of a live chat is dropped from this round, not discarded: its messages stay
    unindexed and come back with whatever follows them."""
    import inspect

    from app.kb.indexer import index_pending

    source = inspect.getsource(index_pending)
    assert "chunks = chunks[:-1]" in source
    # Nothing marks those messages as done before they are actually embedded.
    held = source.index("chunks = chunks[:-1]")
    assert "mark_indexed" not in source[:held]
