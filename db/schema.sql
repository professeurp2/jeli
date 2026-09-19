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
    source      text not null check (source in ('whatsapp_export', 'whatsapp_live', 'telegram', 'recording', 'document')),
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
-- The text of questions asked in groups (never private ones), without author or phone numbers:
-- the team sees what members ask, and what Jeli could not answer.
alter table jeli.events add column if not exists question text;

-- Scheduled jobs that must run once a day at most (the daily digest), even across restarts.
create table if not exists jeli.job_runs (
    job      text not null,
    run_date date not null,
    ran_at   timestamptz not null default now(),
    primary key (job, run_date)
);

-- Control panel: settings changed from the dashboard (they override the environment), who did what,
-- and the passwords members chose themselves (salted scrypt hashes).
create table if not exists jeli.settings (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now(),
    updated_by text not null default ''
);
create table if not exists jeli.audit (
    id     bigint generated always as identity primary key,
    at     timestamptz not null default now(),
    actor  text not null,
    action text not null
);
create index if not exists audit_at on jeli.audit (at desc);
create table if not exists jeli.dashboard_passwords (
    name          text primary key,
    password_hash text not null,
    updated_at    timestamptz not null default now()
);
-- A deadline the team removed stays, dismissed, so that it is never found again.
alter table jeli.deadlines add column if not exists dismissed_at timestamptz;
alter table jeli.deadlines add column if not exists dismissed_by text;

-- Misuse spotted by the guard: floods, repeats, oversized messages, manipulation attempts.
-- member_key is the digits of the member's WhatsApp id (or their name), what the team can block.
create table if not exists jeli.incidents (
    id          bigint generated always as identity primary key,
    at          timestamptz not null default now(),
    member_key  text not null,
    member_name text not null default '',
    kind        text not null
);
create index if not exists incidents_at on jeli.incidents (at desc);

-- Documents members shared in the groups or the team added (PDF, Word, text): the file itself, so
-- Jeli can send it back, and its text as messages of chat_id = the document's id, one per page part,
-- so it is searched and cited like a conversation. A translation Jeli made is kept as a document
-- of its own (translation_of), not indexed, and sent again when asked for again.
create table if not exists jeli.documents (
    id             text primary key,  -- document:<slug>-<hash of the content>
    title          text not null,
    filename       text not null,
    mimetype       text not null,
    size_bytes     integer not null,
    pages          integer not null default 0,
    language       text not null default '',
    shared_by      text not null default '',
    shared_at      timestamptz not null,
    chat_id        text not null default '',  -- where it was shared; '' when added on the dashboard
    content        bytea not null,
    translation_of text references jeli.documents (id) on delete cascade,
    created_at     timestamptz not null default now()
);
create index if not exists documents_translation_of on jeli.documents (translation_of, language);

-- Where each exchange happened (whatsapp, telegram, dashboard) and in which group: the live feed.
alter table jeli.events add column if not exists channel text not null default '';
alter table jeli.events add column if not exists chat_id text not null default '';

-- The team's tries of Jeli on the dashboard, kept per member.
create table if not exists jeli.tries (
    id      bigint generated always as identity primary key,
    member  text not null,
    at      timestamptz not null default now(),
    role    text not null check (role in ('member', 'jeli')),
    text    text not null,
    details jsonb not null default '{}'::jsonb
);
create index if not exists tries_member_at on jeli.tries (member, at);

-- Votes in the groups' polls: one row per voter (their latest choice), for the running tallies.
create table if not exists jeli.poll_votes (
    poll_id  text not null,  -- the poll's message id
    voter    text not null,
    options  text[] not null,
    voted_at timestamptz not null default now(),
    primary key (poll_id, voter)
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
