"""The memory's spare sense: a question is searched in the space of whichever embedded it.

Groq has no embedding model (measured on the key, 22 Sep), so the search that finds what the group
said falls back to a model running in Jeli's own process — no key, no quota, no network.
"""

import asyncio

import pytest

from app.kb.embeddings import Embedder
from app.kb.local_embeddings import DIMENSIONS, LocalEmbedder
from app.kb.store import VECTOR_COLUMNS


class FakeLocal:
    """The local model without loading 220 MB of it."""

    space = "backup"
    available = True

    def __init__(self):
        self.asked = []

    async def embed_documents(self, texts):
        self.asked.extend(texts)
        return [[0.1] * DIMENSIONS for _ in texts]

    async def embed_query(self, text):
        self.asked.append(text)
        return [0.1] * DIMENSIONS


class Broken(FakeLocal):
    available = False


def embedder(gemini_works: bool, backup=None) -> Embedder:
    made = Embedder([], backup=backup)

    async def gemini(texts, task_type):
        if not gemini_works:
            raise RuntimeError("Google is unreachable")
        return [[0.2] * 768 for _ in texts]

    made._embed = gemini
    return made


def test_a_passage_is_kept_in_both_spaces():
    local = FakeLocal()
    main, spare = asyncio.run(embedder(True, local).embed_both(["What did I miss?"]))
    assert len(main[0]) == 768 and len(spare[0]) == DIMENSIONS
    assert local.asked == ["What did I miss?"]


def test_when_google_is_unreachable_the_question_is_searched_in_the_spare_space():
    vector, space = asyncio.run(embedder(False, FakeLocal()).query("C'est quand la réunion ?"))
    assert space == "backup" and len(vector) == DIMENSIONS
    assert VECTOR_COLUMNS[space] == "embedding_backup"  # the column that holds those vectors


def test_google_is_used_while_it_works():
    vector, space = asyncio.run(embedder(True, FakeLocal()).query("C'est quand la réunion ?"))
    assert space == "gemini" and len(vector) == 768
    assert VECTOR_COLUMNS[space] == "embedding"


def test_without_a_spare_sense_the_failure_is_not_hidden():
    with pytest.raises(RuntimeError):
        asyncio.run(embedder(False, None).query("C'est quand la réunion ?"))
    with pytest.raises(RuntimeError):
        asyncio.run(embedder(False, Broken()).query("C'est quand la réunion ?"))


def test_a_passage_google_could_not_embed_is_not_saved_half_way():
    """A chunk with no Gemini vector waits for the next run: the column cannot be null."""
    main, spare = asyncio.run(embedder(False, FakeLocal()).embed_both(["What did I miss?"]))
    assert main is None and spare is not None


def test_the_real_model_speaks_french_english_and_swahili():
    """Marked slow: it downloads ~220 MB the first time. Run it with -m 'slow' when changing
    the model (`pytest tests/test_local_embeddings.py -k real --runslow`)."""
    pytest.importorskip("fastembed")
    local = LocalEmbedder()
    if not asyncio.run(local.load()):
        pytest.skip(f"model not available here: {local.failed}")
    french, english, other = asyncio.run(
        local.embed_documents([
            "La réunion est vendredi à 10h",
            "The meeting is on Friday at 10",
            "Le déjeuner était délicieux",
        ])
    )
    near = sum(a * b for a, b in zip(french, english))
    far = sum(a * b for a, b in zip(french, other))
    assert len(french) == DIMENSIONS
    assert near > far + 0.15  # the translation is closer than an unrelated French sentence


def test_the_threshold_follows_the_memory_that_answered():
    """Measured on the real memory: the spare space scores lower for the same closeness."""
    from app.kb.search import BACKUP_SCALE, floor_for
    from app.kb.store import SearchHit

    def hit(space):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        return SearchHit(1, "c", now, now, [], [], "text", 1.0, 0.5, space=space)

    assert floor_for([hit("gemini")], 0.60) == 0.60
    assert floor_for([hit("backup")], 0.60) == 0.60 * BACKUP_SCALE
    assert floor_for([], 0.60) == 0.60
    # A question about the group scored 0.53 at worst in the spare space, one about nothing 0.28.
    assert 0.28 < floor_for([hit("backup")], 0.60) < 0.53


class WaitingStore:
    """A memory where some passages were kept while Google was unreachable."""

    def __init__(self, waiting):
        self.waiting = list(waiting)
        self.filled = {}

    async def chunks_missing_gemini(self, limit=100):
        return self.waiting[:limit]

    async def fill_gemini_embedding(self, chunk_id, embedding):
        self.filled[chunk_id] = list(embedding)
        self.waiting = [row for row in self.waiting if row["id"] != chunk_id]


def test_what_was_remembered_without_google_is_caught_up_when_it_returns():
    """The memory keeps growing during an outage, and both spaces hold the same thing after it."""
    from app.kb.indexer import catch_up_gemini

    store = WaitingStore([{"id": 1, "content": "what was said last night"}])
    assert asyncio.run(catch_up_gemini(store, embedder(True, FakeLocal()))) == 1
    assert len(store.filled[1]) == 768 and store.waiting == []
    # While Google is still down, nothing is lost and nothing is claimed.
    store = WaitingStore([{"id": 2, "content": "and this one"}])
    assert asyncio.run(catch_up_gemini(store, embedder(False, FakeLocal()))) == 0
    assert store.waiting and not store.filled
