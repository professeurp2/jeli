"""Delete what Jeli remembers: the stored messages and their indexed chunks.

    python -m scripts.forget --chat-id 120363…@g.us --yes
    python -m scripts.forget --all --yes
"""

import argparse

from app.config import get_settings
from app.kb.store import Store
from scripts.common import require, run


async def main(chat_id: str | None) -> None:
    store = Store(require(get_settings().database_url, "DATABASE_URL"))
    await store.open()
    try:
        messages, chunks = await store.forget(chat_id)
    finally:
        await store.close()
    print(f"Deleted {messages} messages and {chunks} chunks{f' of {chat_id}' if chat_id else ''}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--chat-id", help="forget one chat")
    target.add_argument("--all", action="store_true", help="forget everything")
    parser.add_argument("--yes", action="store_true", help="confirm: this cannot be undone")
    args = parser.parse_args()
    if not args.yes:
        parser.error("add --yes to confirm: deleted messages cannot be recovered")
    run(main(args.chat_id))
