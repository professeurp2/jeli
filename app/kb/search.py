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


def keyword_query(question: str) -> str | None:
    """A Postgres tsquery matching any meaningful word of the question, e.g. 'bootcamp | dates'."""
    words = [w for w in re.findall(r"\w+", question.lower()) if len(w) > 2 and w not in STOPWORDS]
    return " | ".join(dict.fromkeys(words)) or None


async def search(store: Store, embedder: Embedder, question: str, limit: int = 5) -> list[SearchHit]:
    embedding = await embedder.embed_query(question)
    return await store.search(embedding, keyword_query(question), limit=limit)
