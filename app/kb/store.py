"""Knowledge base storage: Supabase Postgres + pgvector (schema in db/schema.sql)."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.ingest.chunker import Chunk
from app.models import Deadline, Document, Recording, StoredMessage, UsageEvent

log = logging.getLogger(__name__)

INSERT_BATCH = 1000
# Jeli is in the groups when it received group messages live within this window.
LIVE_WINDOW = timedelta(days=3)

INSERT_MESSAGES = """
insert into jeli.messages (id, chat_id, source, author, author_id, sent_at, text)
select * from unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[], %s::timestamptz[], %s::text[])
on conflict (id) do nothing
"""

# Hybrid retrieval: semantic neighbours and keyword matches, merged by reciprocal rank fusion.
# Keywords are matched with French and English stemming ("échéances" finds "échéance"); the
# chunks' search column is built the same way (db/schema.sql). Recent conversations get a small
# bonus (a quarter more at most, fading over a month): members mostly ask about the latest
# announcement, and an older repeat of the same words must not win by rank alone.
# {column} is "embedding" (Gemini) or "embedding_backup" (the local model): a question is searched
# in the space of whichever embedded it — the two are not comparable. Never user input.
SEARCH = """
with semantic as (
    select id, row_number() over (order by {column} <=> %(embedding)s::vector) as rank
    from jeli.chunks
    where {column} is not null
    order by {column} <=> %(embedding)s::vector
    limit %(candidates)s
),
keyword as (
    select id, row_number() over (order by ts_rank_cd(search, query) desc) as rank
    from jeli.chunks, (select to_tsquery('french', %(keywords)s) || to_tsquery('english', %(keywords)s) || to_tsquery('simple', %(keywords)s)) as q(query)
    where search @@ query
    order by ts_rank_cd(search, query) desc
    limit %(candidates)s
),
fused as (
    select id, sum(1.0 / (60 + rank)) as score
    from (select * from semantic union all select * from keyword) as ranked
    group by id
)
select c.id, c.chat_id, c.started_at, c.ended_at, c.authors, c.message_ids, c.content,
       f.score * (1 + %(recency)s * exp(-greatest(extract(epoch from (now() - c.ended_at)), 0) / (86400.0 * 30))) as score,
       1 - (c.{column} <=> %(embedding)s::vector) as similarity
from fused as f join jeli.chunks as c using (id)
order by 8 desc
limit %(limit)s
"""
RECENCY_BONUS = 0.25
# The two vector spaces the memory keeps, and the column each one lives in.
VECTOR_COLUMNS = {"gemini": "embedding", "backup": "embedding_backup"}


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
    # Which memory answered: "gemini" or "backup" (the local model). The two do not score on the
    # same scale, so what counts as "close enough" depends on it (app/kb/search.py).
    space: str = "gemini"


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

    # --- Documents ------------------------------------------------------------------------------

    DOCUMENT_COLUMNS = "id, title, filename, mimetype, size_bytes, pages, language, shared_by, shared_at, chat_id, translation_of"

    async def save_document(self, document: Document, content: bytes) -> bool:
        """Keep a document's file; False if the same document is already kept."""
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "insert into jeli.documents (id, title, filename, mimetype, size_bytes, pages, language, shared_by, "
                "shared_at, chat_id, translation_of, content) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "on conflict (id) do nothing",
                (
                    document.id, document.title, document.filename, document.mimetype, document.size_bytes,
                    document.pages, document.language, document.shared_by, document.shared_at, document.chat_id,
                    document.translation_of, content,
                ),
            )
        return cursor.rowcount == 1

    async def member_names(self) -> dict[str, str]:
        """Phone number (digits) → the name members see on WhatsApp, learned from live messages:
        exports name unsaved members by their number."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select distinct on (number) number, author from ("
                    "select split_part(author_id, '@', 1) as number, author, sent_at from jeli.messages "
                    "where source = 'whatsapp_live' and author_id like '%%@c.us') as live order by number, sent_at desc"
                )
            ).fetchall()
        return {row["number"]: row["author"] for row in rows if row["author"] and row["author"] != "Someone"}

    async def save_poll_vote(self, poll_id: str, voter: str, options: list[str]) -> None:
        """A member's vote: their latest choice replaces the previous one (an empty choice withdraws it)."""
        async with self._pool.connection() as conn:
            if options:
                await conn.execute(
                    "insert into jeli.poll_votes (poll_id, voter, options) values (%s, %s, %s) on conflict (poll_id, voter) "
                    "do update set options = excluded.options, voted_at = now()",
                    (poll_id, voter, options),
                )
            else:
                await conn.execute("delete from jeli.poll_votes where poll_id = %s and voter = %s", (poll_id, voter))

    async def poll_tallies(self, poll_ids: Sequence[str]) -> dict[str, dict[str, int]]:
        """Votes per option, for each poll."""
        if not poll_ids:
            return {}
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select poll_id, option, count(*) as n from jeli.poll_votes, unnest(options) as option "
                    "where poll_id = any(%s) group by poll_id, option",
                    (list(poll_ids),),
                )
            ).fetchall()
        tallies: dict[str, dict[str, int]] = {}
        for row in rows:
            tallies.setdefault(row["poll_id"], {})[row["option"]] = row["n"]
        return tallies

    async def mentioned_documents(self, limit: int = 60) -> list[dict]:
        """Messages that shared a file the chat history does not include ("<document omis>"),
        newest first: author, when, where, and the message's text."""
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select author, sent_at, chat_id, text from jeli.messages "
                    "where source in ('whatsapp_export', 'whatsapp_live') and (text ilike '%%document omis%%' "
                    "or text ilike '%%document omitted%%' or text ilike '%%<attached:%%' or text ilike '%%(fichier joint)%%' "
                    "or text ilike '%%(file attached)%%' or text ilike '%%<pièce jointe%%') "
                    "order by sent_at desc limit %s",
                    (limit,),
                )
            ).fetchall()

    async def documents(self, ids: Sequence[str]) -> dict[str, Document]:
        if not ids:
            return {}
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(f"select {self.DOCUMENT_COLUMNS} from jeli.documents where id = any(%s)", (list(ids),))
            ).fetchall()
        return {row["id"]: Document(**row) for row in rows}

    async def list_documents(self) -> list[Document]:
        """The documents Jeli keeps (not the translations it made), newest first."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    f"select {self.DOCUMENT_COLUMNS} from jeli.documents where translation_of is null order by shared_at desc"
                )
            ).fetchall()
        return [Document(**row) for row in rows]

    async def document_content(self, document_id: str) -> bytes | None:
        async with self._pool.connection() as conn:
            row = await (await conn.execute("select content from jeli.documents where id = %s", (document_id,))).fetchone()
        return bytes(row["content"]) if row else None

    async def translation(self, document_id: str, language: str) -> Document | None:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    f"select {self.DOCUMENT_COLUMNS} from jeli.documents where translation_of = %s and language = %s",
                    (document_id, language),
                )
            ).fetchone()
        return Document(**row) if row else None

    async def remove_document(self, document_id: str) -> None:
        """Forget a document: its file, its translations, and its text in the knowledge base."""
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute("delete from jeli.messages where chat_id = %s", (document_id,))
            await conn.execute("delete from jeli.chunks where chat_id = %s", (document_id,))
            await conn.execute("delete from jeli.documents where id = %s or translation_of = %s", (document_id, document_id))

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
        """Chat messages (not recording transcripts nor documents) since a moment, oldest first; the newest `limit` if more."""
        chats = "and chat_id = any(%s)" if chat_ids is not None else ""
        params = (since, list(chat_ids), limit) if chat_ids is not None else (since, limit)
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, chat_id, source, author, author_id, sent_at, text from jeli.messages "
                    f"where sent_at >= %s and source not in ('recording', 'document') {chats} order by sent_at desc, id desc limit %s",
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
                "insert into jeli.events (kind, outcome, language, is_private, latency_ms, question, channel, chat_id) "
                "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    event.kind, event.outcome, event.language, event.is_private, event.latency_ms,
                    event.question or None, event.channel, event.chat_id,
                ),
            )

    async def usage_since(self, since: datetime, include_tries: bool = True) -> dict:
        """Counters for the dashboard: interactions by kind and day, question outcomes, reply times.
        Tries from the dashboard count too, unless include_tries is False."""
        channels = "" if include_tries else "and channel <> 'dashboard'"
        async with self._pool.connection() as conn:
            by_kind = await (
                await conn.execute(
                    f"select kind, count(*) as n from jeli.events where at >= %s {channels} group by kind", (since,)
                )
            ).fetchall()
            by_day = await (
                await conn.execute(
                    "select (at at time zone 'utc')::date as day, outcome, count(*) as n from jeli.events "
                    f"where at >= %s and kind = 'question' {channels} group by 1, 2 order by 1",
                    (since,),
                )
            ).fetchall()
            latency = await (
                await conn.execute(
                    "select percentile_cont(0.5) within group (order by latency_ms) as median, "
                    "percentile_cont(0.95) within group (order by latency_ms) as p95 "
                    f"from jeli.events where at >= %s and kind = 'question' and latency_ms is not null {channels}",
                    (since,),
                )
            ).fetchone()
            questions = await (
                await conn.execute(
                    "select at, outcome, question from jeli.events "
                    f"where at >= %s and kind = 'question' and question is not null {channels} order by at desc limit 200",
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

    async def member_by_number(self, digits: str) -> str | None:
        """The name a number writes under in the groups, or None when Jeli has never seen it.

        This is how a member proves who they are on Jeli's public page: not a password, but the
        fact that Jeli has already heard them in the community.
        """
        if not digits:
            return None
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select author, count(*) as n from jeli.messages "
                    "where regexp_replace(split_part(split_part(author_id, '@', 1), ':', 1), '\\D', '', 'g') = %s "
                    "group by author order by n desc limit 1",
                    (digits,),
                )
            ).fetchone()
        return row["author"] if row else None

    async def chunks_missing_gemini(self, limit: int = 100) -> list[dict]:
        """Passages kept while Google was unreachable: they have the local vector, not Gemini's."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select id, content from jeli.chunks where embedding is null order by id limit %s", (limit,)
                )
            ).fetchall()
        return [dict(row) for row in rows]

    async def fill_gemini_embedding(self, chunk_id: int, embedding: Sequence[float]) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "update jeli.chunks set embedding = %s::vector where id = %s", (list(embedding), chunk_id)
            )

    # --- Stickers the groups use -------------------------------------------------------------------

    async def remember_sticker(self, file_url: str, emotion: str, chat_id: str = "") -> None:
        """A sticker seen in a group, with the feeling the model read in it. Jeli answers with the
        group's own stickers rather than a pack of its own: it speaks their visual language."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.stickers (file_url, emotion, chat_id) values (%s, %s, %s) "
                "on conflict (file_url) do update set emotion = excluded.emotion",
                (file_url, emotion, chat_id),
            )

    async def pick_sticker(self, emotion: str) -> str | None:
        """A sticker that says this feeling — the one used longest ago, so Jeli does not repeat
        itself. None when the groups have never used one for it."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "select file_url from jeli.stickers where emotion = %s "
                    "order by used_at nulls first, times_used limit 1",
                    (emotion,),
                )
            ).fetchone()
            if not row:
                return None
            await conn.execute(
                "update jeli.stickers set used_at = now(), times_used = times_used + 1 where file_url = %s",
                (row["file_url"],),
            )
        return row["file_url"]

    async def sticker_count(self) -> dict[str, int]:
        """How many stickers Jeli knows, per feeling — for the dashboard."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute("select emotion, count(*) as n from jeli.stickers group by emotion order by n desc")
            ).fetchall()
        return {row["emotion"]: row["n"] for row in rows}

    # --- Reminders members asked for ---------------------------------------------------------------

    async def add_reminder(self, *, platform: str, chat_id: str, message_id: str, member_key: str, member_id: str,
                           what: str, event_at: datetime | None, remind_at: datetime, language: str, message: str) -> int:
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "insert into jeli.reminders (platform, chat_id, message_id, member_key, member_id, what, event_at, remind_at, "
                    "language, message) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) returning id",
                    (platform, chat_id, message_id, member_key, member_id, what, event_at, remind_at, language, message),
                )
            ).fetchone()
        return row["id"]

    async def active_reminders(self, member_key: str) -> list[dict]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select id, chat_id, what, remind_at from jeli.reminders where member_key = %s "
                    "and sent_at is null and cancelled_at is null order by remind_at",
                    (member_key,),
                )
            ).fetchall()

    async def cancel_reminders(self, member_key: str, chat_id: str) -> int:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(
                "update jeli.reminders set cancelled_at = now() where member_key = %s and chat_id = %s "
                "and sent_at is null and cancelled_at is null",
                (member_key, chat_id),
            )
        return cursor.rowcount

    async def due_reminders(self, now: datetime) -> list[dict]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select id, platform, chat_id, message_id, member_id, what, event_at, remind_at, message from jeli.reminders "
                    "where remind_at <= %s and sent_at is null and cancelled_at is null order by remind_at limit 50",
                    (now,),
                )
            ).fetchall()

    async def upcoming_reminders(self, limit: int = 20) -> list[dict]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select what, remind_at, chat_id from jeli.reminders where sent_at is null and cancelled_at is null "
                    "order by remind_at limit %s",
                    (limit,),
                )
            ).fetchall()

    async def finish_reminder(self, reminder_id: int, sent: bool) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("update jeli.reminders set sent_at = now(), sent = %s where id = %s", (sent, reminder_id))

    async def voice_quota(self, day: date) -> list[dict]:
        """Today's natural voice notes, per key fingerprint and speech model."""
        async with self._pool.connection() as conn:
            return await (
                await conn.execute("select key_id, model, used, exhausted from jeli.voice_quota where day = %s", (day,))
            ).fetchall()

    async def count_voice(self, day: date, key_id: str, model: str, exhausted: bool = False) -> None:
        """One voice note made with this key and model, or its quota found spent for the day."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.voice_quota (day, key_id, model, used, exhausted) values (%s, %s, %s, %s, %s) "
                "on conflict (day, key_id, model) do update set used = jeli.voice_quota.used + excluded.used, "
                "exhausted = jeli.voice_quota.exhausted or excluded.exhausted",
                (day, key_id, model, 0 if exhausted else 1, exhausted),
            )

    async def follows_groups_live(self, within: timedelta = LIVE_WINDOW) -> bool:
        """Whether Jeli is in the groups: it received group messages live lately. A quiet night or
        weekend is not a disconnection."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select exists(select 1 from jeli.messages where source = 'whatsapp_live' "
                    "and chat_id like '%%@g.us' and sent_at > now() - %s) as live",
                    (within,),
                )
            ).fetchone()
        return bool(row["live"])

    async def latest_per_chat(self) -> dict[str, datetime]:
        """The last live message Jeli holds in each group: where to pick the history back up after
        a restart (app/ingest/history.py)."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select chat_id, max(sent_at) as latest from jeli.messages "
                    "where source = 'whatsapp_live' group by chat_id"
                )
            ).fetchall()
        return {row["chat_id"]: row["latest"] for row in rows}

    async def latest_message_at(self) -> datetime | None:
        """The last group message Jeli knows: how far its memory of the groups goes."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select max(sent_at) as latest from jeli.messages where source in ('whatsapp_export', 'whatsapp_live', 'telegram')"
                )
            ).fetchone()
        return row["latest"]

    async def recent_events(self, limit: int = 15) -> list[dict]:
        """The latest exchanges with Jeli, newest first: the dashboard's live feed."""
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select id, at, kind, outcome, language, is_private, latency_ms, question, channel, chat_id "
                    "from jeli.events order by id desc limit %s",
                    (limit,),
                )
            ).fetchall()

    async def activity_today(self, since: datetime) -> dict:
        """Messages Jeli read and exchanges it had since a moment (the start of the day)."""
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select (select count(*) from jeli.messages where source = 'whatsapp_live' and sent_at >= %s) as read, "
                    "(select count(*) from jeli.events where at >= %s) as exchanges, "
                    "(select max(sent_at) from jeli.messages where source = 'whatsapp_live') as last_message",
                    (since, since),
                )
            ).fetchone()

    async def change_marker(self) -> str:
        """Changes when anything the dashboard shows changes: it refreshes only then."""
        async with self._pool.connection() as conn:
            row = await (
                await conn.execute(
                    "select (select max(id) from jeli.events), (select max(id) from jeli.audit), "
                    "(select max(id) from jeli.incidents), (select max(id) from jeli.tries), "
                    "(select count(*) from jeli.deadlines where dismissed_at is null), (select max(id) from jeli.chunks), "
                    "(select count(*) from jeli.documents), (select count(*) from jeli.messages where chunk_id is null)"
                )
            ).fetchone()
        return "-".join(str(value) for value in row.values())

    # --- Tries on the dashboard, kept per team member -------------------------------------------

    async def add_try(self, member: str, role: str, text: str, details: dict | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.tries (member, role, text, details) values (%s, %s, %s, %s)",
                (member, role, text, Jsonb(details or {})),
            )

    async def tries(self, member: str, limit: int = 60) -> list[dict]:
        """A member's conversation with Jeli on the dashboard, oldest first."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select at, role, text, details from jeli.tries where member = %s order by id desc limit %s",
                    (member, limit),
                )
            ).fetchall()
        return list(reversed(rows))

    async def clear_tries(self, member: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("delete from jeli.tries where member = %s", (member,))

    async def knowledge_overview(self) -> dict:
        """What Jeli knows, in counts: per chat, per recording, and the indexing backlog."""
        async with self._pool.connection() as conn:
            chats = await (
                await conn.execute(
                    "select chat_id, count(*) as messages, max(sent_at) as last_message, "
                    "count(*) filter (where source = 'whatsapp_live') as live "
                    "from jeli.messages where source not in ('recording', 'document') group by chat_id order by messages desc"
                )
            ).fetchall()
            recordings = await (
                await conn.execute(
                    "select r.id, r.title, r.recorded_at, r.duration_seconds, r.method, r.source_url, "
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

    async def save_chunk(
        self, chunk: Chunk, embedding: Sequence[float] | None, model: str, backup: Sequence[float] | None = None
    ) -> int:
        """`backup`: the same passage in the local model's space, so the memory stays searchable
        when Google is unreachable (app/kb/local_embeddings.py)."""
        async with self._pool.connection() as conn, conn.transaction():
            row = await (
                await conn.execute(
                    "insert into jeli.chunks (chat_id, source, started_at, ended_at, authors, message_ids, "
                    "content, embedding, embedding_model, embedding_backup) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s, %s::vector) returning id",
                    (
                        chunk.chat_id,
                        chunk.source,
                        chunk.started_at,
                        chunk.ended_at,
                        list(chunk.authors),
                        list(chunk.message_ids),
                        chunk.content,
                        list(embedding) if embedding else None,
                        model,
                        list(backup) if backup else None,
                    ),
                )
            ).fetchone()
            await conn.execute(
                "update jeli.messages set chunk_id = %s where id = any(%s)", (row["id"], list(chunk.message_ids))
            )
        return row["id"]

    async def search(
        self, embedding: Sequence[float], keywords: str | None, limit: int = 5, candidates: int = 20,
        space: str = "gemini",
    ) -> list[SearchHit]:
        column = VECTOR_COLUMNS[space]  # a name from our own table, never from the member
        params = {
            "embedding": list(embedding), "keywords": keywords or "", "limit": limit, "candidates": candidates,
            "recency": RECENCY_BONUS,
        }
        async with self._pool.connection() as conn:
            rows = await (await conn.execute(SEARCH.format(column=column), params)).fetchall()
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
                space=space,
            )
            for row in rows
        ]

    # --- Conversations, kept across restarts -----------------------------------------------------

    async def add_turn(self, chat_id: str, member_key: str, is_private: bool, message: str, reply: str, sources=()) -> None:
        """One exchange with a member. Private ones are kept a day at most, for the thread only."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.conversations (chat_id, member_key, is_private, message, reply, sources) "
                "values (%s, %s, %s, %s, %s, %s)",
                (chat_id, member_key, is_private, message[:2000], reply[:4000], Jsonb(list(sources))),
            )
            await conn.execute("delete from jeli.conversations where is_private and at < now() - interval '1 day'")

    async def turns(self, chat_id: str, member_key: str, since: datetime, limit: int = 8) -> list[dict]:
        """The latest exchanges of a member in a chat since a moment, oldest first."""
        async with self._pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "select at, message, reply, sources from jeli.conversations "
                    "where chat_id = %s and member_key = %s and at >= %s order by id desc limit %s",
                    (chat_id, member_key, since, limit),
                )
            ).fetchall()
        return list(reversed(rows))

    async def forget_turns(self, member_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("delete from jeli.conversations where member_key = %s", (member_key,))

    # --- The community brief, and what Jeli knows of each member ---------------------------------

    async def save_brief(self, key: str, text: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.briefs (key, text) values (%s, %s) on conflict (key) "
                "do update set text = excluded.text, updated_at = now()",
                (key, text),
            )

    async def load_brief(self, key: str) -> dict | None:
        async with self._pool.connection() as conn:
            return await (await conn.execute("select text, updated_at from jeli.briefs where key = %s", (key,))).fetchone()

    async def member(self, member_key: str) -> dict | None:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select member_key, name, language, notes, first_seen, last_seen from jeli.members where member_key = %s",
                    (member_key,),
                )
            ).fetchone()

    async def remember_member(self, member_key: str, name: str = "", language: str = "", notes: dict | None = None) -> None:
        """What Jeli learned of a member: their name as shown, the language they write in, notes
        (their own introduction, what they asked lately). Empty values keep the old ones."""
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.members (member_key, name, language, notes) values (%s, %s, %s, %s) "
                "on conflict (member_key) do update set "
                "name = case when excluded.name <> '' then excluded.name else jeli.members.name end, "
                "language = case when excluded.language <> '' then excluded.language else jeli.members.language end, "
                "notes = jeli.members.notes || excluded.notes, last_seen = now()",
                (member_key, name[:80], language[:8], Jsonb(notes or {})),
            )

    async def forget_member_profile(self, member_key: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("delete from jeli.members where member_key = %s", (member_key,))

    # --- Members' feedback on Jeli's answers ------------------------------------------------------

    async def record_feedback(self, chat_id: str, message_id: str, verdict: str, question: str = "", answer: str = "") -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "insert into jeli.feedback (chat_id, message_id, verdict, question, answer) values (%s, %s, %s, %s, %s)",
                (chat_id, message_id, verdict[:20], question[:1000], answer[:4000]),
            )

    async def feedback_since(self, since: datetime, limit: int = 200) -> list[dict]:
        async with self._pool.connection() as conn:
            return await (
                await conn.execute(
                    "select at, chat_id, verdict, question, answer from jeli.feedback where at >= %s order by id desc limit %s",
                    (since, limit),
                )
            ).fetchall()

    async def drop_chunks(self, chat_id: str | None = None) -> int:
        """Forget the chunks (not the messages) of one chat or all: they are indexed again at the
        next run of the memory activity, with the current chunk format."""
        condition, params = ("where chat_id = %s", (chat_id,)) if chat_id else ("", ())
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(f"update jeli.messages set chunk_id = null {condition}", params)
            return (await conn.execute(f"delete from jeli.chunks {condition}", params)).rowcount

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
