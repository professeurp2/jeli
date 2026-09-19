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
    created_at       timestamptz not null default now()
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
