"""Import a WhatsApp "Export chat" file (.txt or .zip) into Jeli's knowledge base.

    python -m scripts.import_whatsapp_export data/exports/chat.zip --dry-run
    python -m scripts.import_whatsapp_export data/exports/chat.zip --chat-id 120363…@g.us

Importing again, or a newer export of the same chat, only adds the messages not stored yet.
Use the group's WhatsApp id as --chat-id, so that the history and the live messages form one chat.
"""

import argparse
from collections import Counter
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.ingest.chunker import chunk_messages
from app.ingest.whatsapp_export import message_ids, parse_export, read_export
from app.kb.embeddings import Embedder
from app.kb.indexer import index_pending
from app.kb.store import Store
from app.models import StoredMessage
from scripts.common import require, run


def parse_args() -> argparse.Namespace:
    settings = get_settings()
    groups = sorted(settings.whatsapp_groups)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="exported .txt or .zip")
    parser.add_argument(
        "--chat-id",
        default=groups[0] if len(groups) == 1 else None,
        help="group id (…@g.us); defaults to WHATSAPP_GROUP_IDS when it lists a single group",
    )
    parser.add_argument("--timezone", default=settings.export_timezone, help="timezone of the exporting phone")
    parser.add_argument("--month-first", action="store_true", help="dates like 9/12/26 mean 12 September")
    parser.add_argument("--until", type=date.fromisoformat, help="only messages before this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="parse and chunk only: nothing is stored or embedded")
    args = parser.parse_args()
    if not args.chat_id and not args.dry_run:
        parser.error("--chat-id is required (or set WHATSAPP_GROUP_IDS to a single group)")
    return args


async def main(args: argparse.Namespace) -> None:
    exported = parse_export(read_export(args.path), timezone=args.timezone, day_first=not args.month_first)
    if args.until:
        limit = datetime.combine(args.until, time.min, tzinfo=ZoneInfo(args.timezone))
        exported = [m for m in exported if m.sent_at < limit]
    if not exported:
        raise SystemExit("No messages found: check the file, or try --month-first")

    chat_id = args.chat_id or "dry-run"
    messages = [
        StoredMessage(id=id_, chat_id=chat_id, source="whatsapp_export", author=m.author, sent_at=m.sent_at, text=m.text)
        for id_, m in zip(message_ids(chat_id, exported), exported)
    ]
    authors = Counter(m.author for m in messages)
    print(f"{len(messages)} messages from {len(authors)} people, {messages[0].sent_at:%Y-%m-%d} → {messages[-1].sent_at:%Y-%m-%d}")

    if args.dry_run:
        chunks = chunk_messages(messages)
        average = sum(len(c.content) for c in chunks) // len(chunks)
        print(f"{len(chunks)} chunks of {average} characters on average. Dry run: nothing stored.")
        return

    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    embedder = Embedder(require(settings.gemini_api_key, "GEMINI_API_KEY"))
    await store.open()
    try:
        added = await store.add_messages(messages)
        print(f"{added} new messages stored ({len(messages) - added} already known)")
        created = await index_pending(store, embedder)
        print(f"{created} chunks embedded and indexed")
    finally:
        await store.close()


if __name__ == "__main__":
    arguments = parse_args()
    run(main(arguments))
