"""Knowledge base storage: Supabase Postgres + pgvector (schema in db/schema.sql)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.ingest.chunker import Chunk
from app.models import Deadline, Recording, StoredMessage, UsageEvent

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
            # The Supabase pooler closes idle connections: check each one before handing it out.
            # Measured: a connection left idle during long model calls failed with "server closed
            # the connection unexpectedly".
            check=AsyncConnectionPool.check_connection,
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

    async def save_recording(self, recording: Recording) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.recordings (id, title, recorded_at, source_url, duration_seconds, method) "
                "values (%s, %s, %s, %s, %s, %s) on conflict (id) do update set title = excluded.title, "
                "recorded_at = excluded.recorded_at, source_url = excluded.source_url, "
                "duration_seconds = excluded.duration_seconds, method = excluded.method",
                (
                    recording.id,
                    recording.title,
                    recording.recorded_at,
                    recording.source_url,
                    recording.duration_seconds,
                    recording.method,
                ),
            )

    async def _recordings(self, condition: str, params: tuple) -> list[Recording]:
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, title, recorded_at, method, source_url, duration_seconds, recap from jeli.recordings "
                    f"{condition} order by recorded_at",
                    params,
                )
            ).fetchall()
        return [Recording(**row) for row in rows]

    async def recordings(self, ids: Sequence[str]) -> dict[str, Recording]:
        if not ids:
            return {}
        return {r.id: r for r in await self._recordings("where id = any(%s)", (list(ids),))}

    async def recordings_since(self, since: datetime) -> list[Recording]:
        return await self._recordings("where recorded_at >= %s", (since,))

    async def all_recordings(self) -> list[Recording]:
        return await self._recordings("", ())

    async def save_recap(self, recording_id: str, language: str, recap: dict) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "update jeli.recordings set recap = coalesce(recap, '{}'::jsonb) || jsonb_build_object(%s::text, %s::jsonb) "
                "where id = %s",
                (language, Jsonb(recap), recording_id),
            )

    async def messages_of(self, chat_id: str) -> list[StoredMessage]:
        """Every message of a chat (or segment of a recording), in time order."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    "where chat_id = %s order by sent_at, id",
                    (chat_id,),
                )
            ).fetchall()
        return [StoredMessage(**row) for row in rows]

    async def messages_since(
        self, since: datetime, chat_ids: Sequence[str] | None = None, limit: int = 1500
    ) -> list[StoredMessage]:
        """Chat messages (not recording transcripts) since a moment, oldest first; the newest `limit` if more."""
        chats = "and chat_id = any(%s)" if chat_ids is not None else ""
        params = (since, list(chat_ids), limit) if chat_ids is not None else (since, limit)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    f"where sent_at >= %s and source <> 'recording' {chats} order by sent_at desc, id desc limit %s",
                    params,
                )
            ).fetchall()
        return [StoredMessage(**row) for row in reversed(rows)]

    async def unchecked_messages(self, limit: int = 150) -> list[StoredMessage]:
        """Messages and transcript segments not yet scanned for deadlines, oldest first."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    "where not deadlines_checked order by sent_at, id limit %s",
                    (limit,),
                )
            ).fetchall()
        return [StoredMessage(**row) for row in rows]

    async def mark_deadlines_checked(self, ids: Sequence[str]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("update jeli.messages set deadlines_checked = true where id = any(%s)", (list(ids),))

    async def add_deadlines(self, deadlines: Sequence[Deadline]) -> int:
        """Store new deadlines; one already known (same day, same wording) is skipped."""
        added = 0
        async with self._pool.connection() as conn:
            for d in deadlines:
                cursor = await conn.execute(
                    "insert into jeli.deadlines (what, due_date, due_time, programme, chat_id, message_id, "
                    "announced_at, author) values (%s, %s, %s, %s, %s, %s, %s, %s) on conflict do nothing",
                    (d.what, d.due_date, d.due_time, d.programme, d.chat_id, d.message_id, d.announced_at, d.author),
                )
                added += cursor.rowcount
        return added

    async def deadlines_between(self, start: date, end: date, include_dismissed: bool = False) -> list[Deadline]:
        """Deadlines due between two days. Those the team removed only with include_dismissed, so
        that extraction knows them and never finds them again."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, what, due_date, chat_id, announced_at, due_time, programme, message_id, author "
                    "from jeli.deadlines where due_date between %s and %s and (%s or dismissed_at is null) "
                    "order by due_date, due_time, announced_at",
                    (start, end, include_dismissed),
                )
            ).fetchall()
        return [Deadline(**row) for row in rows]

    async def dismiss_deadline(self, deadline_id: int, actor: str) -> str | None:
        """Remove a deadline from every list; returns what it was, or None if unknown."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "update jeli.deadlines set dismissed_at = now(), dismissed_by = %s "
                    "where id = %s and dismissed_at is null returning what",
                    (actor, deadline_id),
                )
            ).fetchone()
        return row["what"] if row else None

    # --- Control panel -------------------------------------------------------------------------

    async def load_settings(self) -> dict:
        async with self._pool.connection() as conn:
            rows = await (await conn.execute("select key, value from jeli.settings")).fetchall()
        return {row["key"]: row["value"] for row in rows}

    async def save_settings(self, values: dict, actor: str) -> None:
        async with self._pool.connection() as conn:
            for key, value in values.items():
                await conn.execute(
                    "insert into jeli.settings (key, value, updated_by) values (%s, %s, %s) on conflict (key) "
                    "do update set value = excluded.value, updated_at = now(), updated_by = excluded.updated_by",
                    (key, Jsonb(value), actor),
                )

    async def add_audit(self, actor: str, action: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("insert into jeli.audit (actor, action) values (%s, %s)", (actor, action))

    async def audit_log(self, limit: int = 100) -> list[dict]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute("select at, actor, action from jeli.audit order by at desc limit %s", (limit,))
            ).fetchall()

    async def password_hashes(self) -> dict[str, str]:
        """Passwords members chose themselves; they replace the ones they were given."""
        async with self._pool.connection() as conn:
            rows = await (await conn.execute("select name, password_hash from jeli.dashboard_passwords")).fetchall()
        return {row["name"]: row["password_hash"] for row in rows}

    async def set_password_hash(self, name: str, password_hash: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.dashboard_passwords (name, password_hash) values (%s, %s) on conflict (name) "
                "do update set password_hash = excluded.password_hash, updated_at = now()",
                (name, password_hash),
            )

    async def record_incident(self, member_key: str, member_name: str, kind: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.incidents (member_key, member_name, kind) values (%s, %s, %s)",
                (member_key, member_name, kind),
            )

    async def incidents_since(self, since: datetime) -> list[dict]:
        """Per member: what they did (kind → count), their latest name, and when it last happened."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select member_key, kind, count(*) as n, max(at) as last_at, "
                    "(array_agg(member_name order by at desc))[1] as member_name "
                    "from jeli.incidents where at >= %s group by member_key, kind",
                    (since,),
                )
            ).fetchall()
        members: dict[str, dict] = {}
        for row in rows:
            member = members.setdefault(
                row["member_key"], {"member_key": row["member_key"], "member_name": "", "kinds": {}, "last_at": row["last_at"]}
            )
            member["kinds"][row["kind"]] = row["n"]
            if row["last_at"] >= member["last_at"]:
                member["last_at"], member["member_name"] = row["last_at"], row["member_name"] or member["member_name"]
        return sorted(members.values(), key=lambda m: m["last_at"], reverse=True)

    async def forgive(self, member_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("delete from jeli.incidents where member_key = %s", (member_key,))

    async def questions_since(self, since: datetime, limit: int = 500) -> list[tuple[datetime, str, str]]:
        """Questions asked in groups, newest first: (when, outcome, question)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select at, outcome, question from jeli.events where at >= %s and kind = 'question' "
                    "and question is not null order by at desc limit %s",
                    (since, limit),
                )
            ).fetchall()
        return [(row["at"], row["outcome"], row["question"]) for row in rows]

    async def live_groups(self, since: datetime) -> list[str]:
        """Groups where Jeli received messages since then: where the daily summary goes by default."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select distinct chat_id from jeli.messages where source = 'whatsapp_live' "
                    "and chat_id like '%%@g.us' and sent_at >= %s order by chat_id",
                    (since,),
                )
            ).fetchall()
        return [row["chat_id"] for row in rows]

    async def record_event(self, event: UsageEvent) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.events (kind, outcome, language, is_private, latency_ms, question) "
                "values (%s, %s, %s, %s, %s, %s)",
                (event.kind, event.outcome, event.language, event.is_private, event.latency_ms, event.question or None),
            )

    async def usage_since(self, since: datetime) -> dict:
        """Counters for the dashboard: interactions by kind and day, question outcomes, reply times."""
        async with self._pool.connection() as conn:
            by_kind = await (
                await conn.execute(
                    "select kind, count(*) as n from jeli.events where at >= %s group by kind", (since,)
                )
            ).fetchall()
            by_day = await (
                await conn.execute(
                    "select (at at time zone 'utc')::date as day, outcome, count(*) as n from jeli.events "
                    "where at >= %s and kind = 'question' group by 1, 2 order by 1",
                    (since,),
                )
            ).fetchall()
            latency = await (
                await conn.execute(
                    "select percentile_cont(0.5) within group (order by latency_ms) as median, "
                    "percentile_cont(0.95) within group (order by latency_ms) as p95 "
                    "from jeli.events where at >= %s and kind = 'question' and latency_ms is not null",
                    (since,),
                )
            ).fetchone()
            questions = await (
                await conn.execute(
                    "select at, outcome, question from jeli.events "
                    "where at >= %s and kind = 'question' and question is not null order by at desc limit 200",
                    (since,),
                )
            ).fetchall()
        return {
            "by_kind": {row["kind"]: row["n"] for row in by_kind},
            "questions_by_day": [(row["day"], row["outcome"], row["n"]) for row in by_day],
            "median_ms": latency["median"],
            "p95_ms": latency["p95"],
            "group_questions": [(row["at"], row["outcome"], row["question"]) for row in questions],
        }

    async def knowledge_overview(self) -> dict:
        """What Jeli knows, in counts: per chat, per recording, and the indexing backlog."""
        async with self._pool.connection() as conn:
            chats = await (
                await conn.execute(
                    "select chat_id, count(*) as messages, max(sent_at) as last_message, "
                    "count(*) filter (where source = 'whatsapp_live') as live "
                    "from jeli.messages where source <> 'recording' group by chat_id order by messages desc"
                )
            ).fetchall()
            recordings = await (
                await conn.execute(
                    "select r.title, r.recorded_at, r.duration_seconds, "
                    "coalesce((select array_agg(k order by k) from jsonb_object_keys(r.recap) as k), '{}') as recaps, "
                    "(select count(*) from jeli.messages m where m.chat_id = r.id) as segments "
                    "from jeli.recordings r order by r.recorded_at"
                )
            ).fetchall()
            totals = await (
                await conn.execute(
                    "select (select count(*) from jeli.chunks) as chunks, "
                    "(select count(*) from jeli.messages where chunk_id is null) as pending, "
                    "(select count(*) from jeli.deadlines where due_date >= (now() at time zone 'utc')::date) as deadlines"
                )
            ).fetchone()
        return {"chats": chats, "recordings": recordings, **totals}

    async def claim_daily_run(self, job: str, day: date) -> bool:
        """True the first time a job claims a day: a daily message is never sent twice, even across restarts."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "insert into jeli.job_runs (job, run_date) values (%s, %s) on conflict do nothing", (job, day)
            )
        return cursor.rowcount == 1

    async def release_daily_run(self, job: str, day: date) -> None:
        """Undo a claim whose message could not be sent, so a later run may send it."""
        async with self._pool.connection() as conn:
            await conn.execute("delete from jeli.job_runs where job = %s and run_date = %s", (job, day))

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
        """Delete stored messages and chunks (and the recording, for a recording's id), for one chat
        or everything. Returns (messages, chunks)."""
        condition, params = ("where chat_id = %s", (chat_id,)) if chat_id else ("", ())
        recording_condition = "where id = %s" if chat_id else ""
        async with self._pool.connection() as conn, conn.transaction():
            messages = (await conn.execute(f"delete from jeli.messages {condition}", params)).rowcount
            chunks = (await conn.execute(f"delete from jeli.chunks {condition}", params)).rowcount
            await conn.execute(f"delete from jeli.recordings {recording_condition}", params)
        return messages, chunks
