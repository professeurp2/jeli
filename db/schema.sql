-- Jeli knowledge base (Supabase / Postgres + pgvector).
--
-- Run once (Supabase SQL editor, or a migration), then give the application role a password:
--   alter role jeli_app with login password '<random secret>';
-- and connect Jeli through the pooler with user "jeli_app.<project-ref>" (see README).
--
-- Everything lives in the "jeli" schema, which Supabase's Data API does not expose:
-- only the jeli_app role can read or write it.

create extension if not exists vector with schema extensions;

create schema if not exists jeli;

-- Every chat message Jeli knows about, from a WhatsApp export or received live, and every
-- transcript segment of a call recording (then chat_id is the recording's id).
create table if not exists jeli.messages (
    id          text primary key,  -- platform message id, or a stable fingerprint for exports
    chat_id     text not null,     -- e.g. the WhatsApp group id (…@g.us), or recording:<slug>
    source      text not null check (source in ('whatsapp_export', 'whatsapp_live', 'telegram', 'recording')),
    author      text not null,
    author_id   text,
    sent_at     timestamptz not null,
    text        text not null,
    chunk_id    bigint,            -- set once the message is indexed
    created_at  timestamptz not null default now()
);

create index if not exists messages_chat_sent_at on jeli.messages (chat_id, sent_at);
create index if not exists messages_pending on jeli.messages (chat_id, sent_at) where chunk_id is null;

-- Consecutive messages of one conversation, embedded together: the unit of retrieval.
create table if not exists jeli.chunks (
    id              bigint generated always as identity primary key,
    chat_id         text not null,
    source          text not null,
    started_at      timestamptz not null,
    ended_at        timestamptz not null,
    authors         text[] not null,
    message_ids     text[] not null,
    content         text not null,
    embedding       extensions.vector(768) not null,
    embedding_model text not null,
    -- Keyword search next to the semantic one: names, acronyms, dates.
    search          tsvector generated always as (to_tsvector('simple', content)) stored,
    created_at      timestamptz not null default now()
);

create index if not exists chunks_embedding on jeli.chunks using hnsw (embedding extensions.vector_cosine_ops);
create index if not exists chunks_search on jeli.chunks using gin (search);
create index if not exists chunks_chat_started_at on jeli.chunks (chat_id, started_at);

do $$
begin
    if not exists (select from pg_constraint where conname = 'messages_chunk_id_fkey') then
        alter table jeli.messages
            add constraint messages_chunk_id_fkey foreign key (chunk_id) references jeli.chunks (id) on delete set null;
    end if;
end $$;

create index if not exists messages_chunk_id on jeli.messages (chunk_id);

-- Call recordings: their transcript segments are stored in jeli.messages with chat_id = id.
create table if not exists jeli.recordings (
    id               text primary key,  -- recording:<slug>
    title            text not null,
    recorded_at      timestamptz not null,
    source_url       text,              -- YouTube link (answers link to the exact moment) or file name
    duration_seconds integer,
    method           text not null,     -- how the transcript was made: gemini, subtitles
    recap            jsonb,             -- session recap per language: {"en": {...}, "fr": {...}}
    created_at       timestamptz not null default now()
);

-- Deadlines found in chats and calls (R14), each linked to the message that announced it.
alter table jeli.messages add column if not exists deadlines_checked boolean not null default false;
create index if not exists messages_deadlines_unchecked on jeli.messages (sent_at) where not deadlines_checked;

create table if not exists jeli.deadlines (
    id           bigint generated always as identity primary key,
    what         text not null,
    due_date     date not null,
    due_time     text not null default '',  -- as announced, e.g. "2:00 PM CAT"
    programme    text not null default '',
    chat_id      text not null,
    message_id   text references jeli.messages (id) on delete cascade,
    announced_at timestamptz not null,
    author       text not null default '',
    created_at   timestamptz not null default now()
);
create unique index if not exists deadlines_unique on jeli.deadlines (due_date, lower(what));
create index if not exists deadlines_message_id on jeli.deadlines (message_id);

-- Usage counters for the dashboard (R13): what was asked and how it went — never text or authors.
create table if not exists jeli.events (
    id         bigint generated always as identity primary key,
    at         timestamptz not null default now(),
    kind       text not null,             -- question, catchup, recap, deadlines, search, already_answered, help
    outcome    text not null default '',  -- for questions: answered, dont_know, sources_only, not_ready
    language   text not null default '',
    is_private boolean not null default false,
    latency_ms integer
);
create index if not exists events_at on jeli.events (at);

-- Scheduled jobs that must run once a day at most (the daily digest), even across restarts.
create table if not exists jeli.job_runs (
    job      text not null,
    run_date date not null,
    ran_at   timestamptz not null default now(),
    primary key (job, run_date)
);

-- Least-privilege application role: data access to the jeli schema only.
do $$
begin
    if not exists (select from pg_roles where rolname = 'jeli_app') then
        create role jeli_app nologin;
    end if;
end $$;

grant usage on schema jeli to jeli_app;
grant usage on schema extensions to jeli_app;
grant select, insert, update, delete on all tables in schema jeli to jeli_app;
grant usage, select on all sequences in schema jeli to jeli_app;
alter default privileges in schema jeli grant select, insert, update, delete on tables to jeli_app;
alter default privileges in schema jeli grant usage, select on sequences to jeli_app;
alter role jeli_app set search_path = jeli, extensions, public;
