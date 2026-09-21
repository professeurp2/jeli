"""Clean Jeli's memory: duplicates, aliases, bots, settings — then have it indexed again.

    python -m scripts.hygiene              # dry run: says what it would do, changes nothing
    python -m scripts.hygiene --apply      # does it

Measured on 21 Sep 2026: the main group's history had been imported three times under three
chat ids (meti-cohort-2026, from18-20-september, meti-cohort-before-jeli-joined) on top of the
live group; 611 messages existed in several chats and 945 more twice in the same chat, so the
excerpts given to the model were half repeats. Another bot ("Nexus Bot") was not among the
ignored authors, and the deadline extraction had been switched off.

What it does, in order:
1. Merge the alias chats into one (--merge "a,b:target"; default: the three METI aliases into
   meti-cohort-2026), so citations and labels agree.
2. Delete repeats: same author, same time, same text within a chat (the copy a deadline refers
   to is kept; others' deadlines are re-pointed), and export copies of messages the live group
   (--live) already holds, same minute and text.
3. Add the other bots found by name to the ignored authors (--bot "Nexus Bot", repeatable).
4. Switch the deadline extraction back on (--keep-deadlines-off to leave it).
5. Drop every chunk: the running Jeli indexes everything again within minutes, in the current
   chunk format, without the bots (--keep-chunks to skip; --reindex-here to embed from this
   machine instead of waiting for the server).
"""

import argparse
import asyncio
import json
import re
import sys

import psycopg
from psycopg.types.json import Jsonb

from app.config import get_settings
from scripts.common import require

DEFAULT_MERGE = "from18-20-september,meti-cohort-before-jeli-joined:meti-cohort-2026"
DEFAULT_LIVE = "120363429618850959@g.us"
DEFAULT_BOTS = ["Nexus Bot"]


def parse_merge(value: str) -> tuple[list[str], str]:
    aliases, _, target = value.partition(":")
    return [a.strip() for a in aliases.split(",") if a.strip()], target.strip()


def count(conn, sql: str, params=()) -> int:
    return conn.execute(sql, params).fetchone()[0]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--merge", default=DEFAULT_MERGE, help='"alias1,alias2:target" ("" for none)')
    parser.add_argument("--live", default=DEFAULT_LIVE, help="live chat id the merged export duplicates ('' for none)")
    parser.add_argument("--bot", action="append", default=None, help="display name of another bot to ignore (repeatable)")
    parser.add_argument("--keep-deadlines-off", action="store_true")
    parser.add_argument("--keep-chunks", action="store_true")
    parser.add_argument("--reindex-here", action="store_true", help="embed the chunks from this machine now")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    settings = get_settings()
    url = require(settings.database_url, "DATABASE_URL")
    bots = args.bot if args.bot is not None else DEFAULT_BOTS
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}]")

    with psycopg.connect(url, autocommit=False) as conn:
        total = count(conn, "select count(*) from jeli.messages where source in ('whatsapp_export', 'whatsapp_live')")
        print(f"chat messages before: {total}")

        # 1. Aliases → one chat.
        if args.merge:
            aliases, target = parse_merge(args.merge)
            n = count(conn, "select count(*) from jeli.messages where chat_id = any(%s)", (aliases,))
            print(f"1. merge {aliases} into {target}: {n} messages")
            if args.apply and n:
                conn.execute("update jeli.messages set chat_id = %s where chat_id = any(%s)", (target, aliases))
                conn.execute("update jeli.deadlines set chat_id = %s where chat_id = any(%s)", (target, aliases))
                conn.execute("update jeli.documents set chat_id = %s where chat_id = any(%s)", (target, aliases))
                conn.execute("delete from jeli.chunks where chat_id = any(%s)", (aliases,))

        # 2a. Repeats within a chat.
        repeats = conn.execute(
            "select author, sent_at, text, array_agg(id order by id) as ids from jeli.messages "
            "where source in ('whatsapp_export', 'whatsapp_live') group by 1, 2, 3 having count(*) > 1"
        ).fetchall()
        extra = sum(len(ids) - 1 for _, _, _, ids in repeats)
        print(f"2a. repeats within a chat: {len(repeats)} messages, {extra} extra rows")
        if args.apply and repeats:
            for _, _, _, ids in repeats:
                referenced = conn.execute(
                    "select distinct message_id from jeli.deadlines where message_id = any(%s)", (ids,)
                ).fetchall()
                keep = referenced[0][0] if referenced else ids[0]
                others = [i for i in ids if i != keep]
                conn.execute("update jeli.deadlines set message_id = %s where message_id = any(%s)", (keep, others))
                conn.execute("delete from jeli.messages where id = any(%s)", (others,))

        # 2b. Export copies of what the live group already holds.
        if args.live:
            dupes = conn.execute(
                "select e.id from jeli.messages e join jeli.messages l "
                "on l.chat_id = %s and l.source = 'whatsapp_live' and e.source = 'whatsapp_export' "
                "and date_trunc('minute', e.sent_at) = date_trunc('minute', l.sent_at) and e.text = l.text "
                "where length(e.text) > 20",
                (args.live,),
            ).fetchall()
            ids = [row[0] for row in dupes]
            print(f"2b. export copies of live messages in {args.live}: {len(ids)}")
            if args.apply and ids:
                conn.execute("update jeli.deadlines set message_id = null where message_id = any(%s)", (ids,))
                conn.execute("delete from jeli.messages where id = any(%s)", (ids,))

        # 3. Other bots.
        row = conn.execute("select value from jeli.settings where key = 'ignored_authors'").fetchone()
        ignored = list(row[0]) if row else list(settings.ignored_author_list)
        additions = []
        for bot in bots:
            numbers = conn.execute(
                "select distinct split_part(author_id, '@', 1) from jeli.messages where author = %s and author_id is not null",
                (bot,),
            ).fetchall()
            for entry in [bot, *[re.sub(r"\D", "", n[0]) for n in numbers if n[0]]]:
                if entry and entry not in ignored and entry.lower() not in {i.lower() for i in ignored}:
                    additions.append(entry)
        print(f"3. ignored authors to add: {additions or 'none'} (now: {len(ignored)})")
        if args.apply and additions:
            conn.execute(
                "insert into jeli.settings (key, value, updated_by) values ('ignored_authors', %s, 'hygiene') "
                "on conflict (key) do update set value = excluded.value, updated_at = now(), updated_by = 'hygiene'",
                (Jsonb(ignored + additions),),
            )

        # 4. Deadlines back on.
        row = conn.execute("select value from jeli.settings where key = 'enabled.deadlines'").fetchone()
        off = row is not None and row[0] is False
        print(f"4. deadline extraction: {'OFF → on' if off and not args.keep_deadlines_off else 'unchanged'}")
        if args.apply and off and not args.keep_deadlines_off:
            conn.execute(
                "insert into jeli.settings (key, value, updated_by) values ('enabled.deadlines', 'true'::jsonb, 'hygiene') "
                "on conflict (key) do update set value = 'true'::jsonb, updated_at = now(), updated_by = 'hygiene'"
            )
            conn.execute("insert into jeli.audit (actor, action) values ('hygiene', 'Switched the deadline finding back on')")

        # 5. Chunks.
        chunks = count(conn, "select count(*) from jeli.chunks")
        print(f"5. chunks to drop for re-indexing: {chunks}{' (kept)' if args.keep_chunks else ''}")
        if args.apply and not args.keep_chunks:
            conn.execute("update jeli.messages set chunk_id = null")
            conn.execute("delete from jeli.chunks")

        if args.apply:
            conn.commit()
            after = count(conn, "select count(*) from jeli.messages where source in ('whatsapp_export', 'whatsapp_live')")
            print(f"chat messages after: {after} ({total - after} removed)")
        else:
            conn.rollback()
            print("nothing changed (dry run). Add --apply to do it.")

    if args.apply and args.reindex_here and not args.keep_chunks:
        from app.kb.embeddings import Embedder
        from app.kb.indexer import index_pending
        from app.kb.store import Store
        from app.answer.citations import ignored_keys
        from scripts.common import run

        async def reindex() -> None:
            store = Store(url)
            await store.open()
            try:
                labels = (await store.load_settings()).get("chat_labels") or settings.chat_label_map
                ignored_now = (await store.load_settings()).get("ignored_authors") or settings.ignored_author_list
                created = await index_pending(store, Embedder(settings.api_key_list), ignored=ignored_keys(ignored_now), labels=labels)
                print(f"re-indexed: {created} chunks")
            finally:
                await store.close()

        run(reindex())


if __name__ == "__main__":
    main()
