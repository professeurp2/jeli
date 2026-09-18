"""Knowledge base storage: Supabase Postgres + pgvector (schema in db/schema.sql)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.ingest.chunker import Chunk
from app.models import StoredMessage

log = logging.getLogger(__name__)

INSERT_BATCH = 1000

INSERT_MESSAGES = """
insert into jeli.messages (id, chat_id, source, author, author_id, sent_at, text)
select * from unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::text[])
on conflict (id) do nothing
"""

# Hybrid retrieval: semantic neighbours and keyword matches, merged by reciprocal rank fusion.
SEARCH = """
with semantic as (
    select id, row_number() over (order by embedding <=> %(embedding)s::vector) as rank
    from jeli.chunks
    order by embedding <=> %(embedding)s::vector
    limit %(candidates)s
),
keyword as (
    select id, row_number() over (order by ts_rank_cd(search, query) desc) as rank
    from jeli.chunks, to_tsquery('simple', %(keywords)s) as query
    where search @@ query
    order by ts_rank_cd(search, query) desc
    limit %(candidates)s
),
fused as (
    select id, sum(1.0 / (60 + rank)) as score
    from (select * from semantic union all select * from keyword) as ranked
    group by id
)
select c.id, c.chat_id, c.started_at, c.ended_at, c.authors, c.message_ids, c.content, f.score,
       1 - (c.embedding <=> %(embedding)s::vector) as similarity
from fused as f join jeli.chunks as c using (id)
order by f.score desc
limit %(limit)s
"""


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    chat_id: str
    started_at: datetime
    ended_at: datetime
    authors: list[str]
    message_ids: list[str]
    content: str
    score: float  # fused rank score, only meaningful for ordering
    similarity: float  # cosine similarity to the question, 0..1


class Store:
    def __init__(self, database_url: str, max_size: int = 4):
        self._pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=max_size,
            open=False,
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

    async def open(self) -> None:
        try:
            await self._pool.open(wait=True, timeout=15)
        except PoolTimeout:
            # The pool keeps retrying in the background: Jeli still answers what it can meanwhile.
            log.error("Database unreachable at startup, still retrying")

    async def close(self) -> None:
        await self._pool.close()

    async def add_messages(self, messages: Sequence[StoredMessage]) -> int:
        """Store messages not known yet; returns how many were new."""
        added = 0
        async with self._pool.connection() as conn:
            for start in range(0, len(messages), INSERT_BATCH):
                batch = messages[start : start + INSERT_BATCH]
                columns = (
                    [m.id for m in batch],
                    [m.chat_id for m in batch],
                    [m.source for m in batch],
                    [m.author for m in batch],
                    [m.author_id for m in batch],
                    [m.sent_at for m in batch],
                    [m.text for m in batch],
                )
                cursor = await conn.execute(INSERT_MESSAGES, columns)
                added += cursor.rowcount
        return added

    async def pending_chats(self) -> list[str]:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute("select distinct chat_id from jeli.messages where chunk_id is null")).fetchall()
        return [row["chat_id"] for row in rows]

    async def pending_messages(self, chat_id: str) -> list[StoredMessage]:
        """Messages of a chat that are not part of any chunk yet, in time order."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    "where chat_id = %s and chunk_id is null order by sent_at, id",
                    (chat_id,),
                )
            ).fetchall()
        return [StoredMessage(**row) for row in rows]

    async def messages_by_ids(self, ids: Sequence[str]) -> list[StoredMessage]:
        """The messages behind retrieved chunks, in time order: who said what, and when."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    "where id = any(%s) order by sent_at, id",
                    (list(ids),),
                )
            ).fetchall()
        return [StoredMessage(**row) for row in rows]

    async def save_chunk(self, chunk: Chunk, embedding: Sequence[float], model: str) -> int:
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "insert into jeli.chunks (chat_id, source, started_at, ended_at, authors, message_ids, "
                    "content, embedding, embedding_model) values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s) "
                    "returning id",
                    (
                        chunk.chat_id,
                        chunk.source,
                        chunk.started_at,
                        chunk.ended_at,
                        list(chunk.authors),
                        list(chunk.message_ids),
                        chunk.content,
                        list(embedding),
                        model,
                    ),
                )
            ).fetchone()
            await conn.execute(
                "update jeli.messages set chunk_id = %s where id = any(%s)", (row["id"], list(chunk.message_ids))
            )
        return row["id"]

    async def search(
        self, embedding: Sequence[float], keywords: str | None, limit: int = 5, candidates: int = 20
    ) -> list[SearchHit]:
        params = {"embedding": list(embedding), "keywords": keywords, "limit": limit, "candidates": candidates}
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(SEARCH, params)).fetchall()
        return [
            SearchHit(
                chunk_id=row["id"],
                chat_id=row["chat_id"],
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                authors=row["authors"],
                message_ids=row["message_ids"],
                content=row["content"],
                score=float(row["score"]),
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]

    async def forget(self, chat_id: str | None = None) -> tuple[int, int]:
        """Delete stored messages and chunks, for one chat or everything. Returns (messages, chunks)."""
        condition, params = ("where chat_id = %s", (chat_id,)) if chat_id else ("", ())
        async with self._pool.connection() as conn, conn.transaction():
            messages = (await conn.execute(f"delete from jeli.messages {condition}", params)).rowcount
            chunks = (await conn.execute(f"delete from jeli.chunks {condition}", params)).rowcount
        return messages, chunks
