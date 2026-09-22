"""Find the chunks of conversation that best match a question."""

import re

from app.kb.embeddings import Embedder
from app.kb.store import SearchHit, Store

# Frequent English and French words that would match nearly every chunk.
STOPWORDS = set(
    """
    the and for are was were what when where who why how which with this that from have has had will
    about into your you our they them their there here been can could would should did does done not
    les des une est sont pour que qui quoi quand comment pourquoi avec dans sur par pas plus mais
    ont été sera nous vous ils elles leur cette ces aux du au en et la le un
    """.split()
)
# The chunks semantically closest to the question are always kept.
GUARANTEED_SEMANTIC = 2


def keyword_query(question: str) -> str | None:
    """A Postgres tsquery matching any meaningful word of the question, e.g. 'bootcamp | dates'."""
    words = [w for w in re.findall(r"\w+", question.lower()) if len(w) > 2 and w not in STOPWORDS]
    return " | ".join(dict.fromkeys(words)) or None


def select(hits: list[SearchHit], limit: int) -> list[SearchHit]:
    """The best `limit` hits in fused order, always including the semantically closest ones.

    Keyword matches must not crowd out the best semantic match. Measured: a French question over
    English transcripts matched only "module" as a keyword, which every recording chunk contains,
    and pushed the one excerpt answering it (similarity 0.72, the highest) down to 10th place.
    """
    closest = sorted(hits, key=lambda hit: hit.similarity, reverse=True)[:GUARANTEED_SEMANTIC]
    chosen = (closest + [hit for hit in hits if hit not in closest])[:limit]
    return [hit for hit in hits if hit in chosen]


# The local model scores lower than Gemini for the same closeness. Measured on the real memory
# (909 passages, 22 Sep): questions about the group scored 0.53 to 0.65 in the spare space, and
# questions with nothing to do with it 0.15 to 0.28 — a wide gap, but around 0.45, not 0.60. Every
# configured threshold is scaled by this when the spare memory answered.
BACKUP_SCALE = 0.75


def floor_for(hits: list[SearchHit], configured: float) -> float:
    """The similarity a hit must reach, on the scale of the memory that found it."""
    return configured * BACKUP_SCALE if hits and hits[0].space == "backup" else configured


async def search(store: Store, embedder: Embedder, question: str, limit: int = 5) -> list[SearchHit]:
    embedding, space = (
        await embedder.query(question) if hasattr(embedder, "query")
        else (await embedder.embed_query(question), "gemini")
    )
    candidates = await store.search(embedding, keyword_query(question), limit=limit * 3, space=space)
    return select(candidates, limit)
