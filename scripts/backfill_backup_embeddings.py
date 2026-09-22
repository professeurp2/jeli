"""Give every remembered passage its vector in the local model's space.

The memory was built with Gemini's embeddings. When Google is unreachable, those vectors cannot be
searched — and Groq has no embedding model to take over (checked on the key, 22 Sep). Each passage
therefore also gets a vector from the model running on our own server; this fills that column for
everything remembered before it existed.

    python -m scripts.backfill_backup_embeddings [--batch 64]

Safe to stop and run again: it only touches rows that have no spare vector yet.
"""

import argparse
import logging

from app.config import get_settings
from app.kb.local_embeddings import DIMENSIONS, LocalEmbedder
from app.kb.store import Store
from scripts.common import require, run

log = logging.getLogger("backfill")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=64, help="passages embedded per round")
    args = parser.parse_args()

    settings = get_settings()
    require(settings.database_url, "DATABASE_URL")
    local = LocalEmbedder()
    if not await local.load():
        raise SystemExit(f"the local model could not be loaded: {local.failed}")

    store = Store(settings.database_url)
    await store.open()
    try:
        async with store._pool.connection() as conn:
            total = (await (await conn.execute(
                "select count(*) as n from jeli.chunks where embedding_backup is null"
            )).fetchone())["n"]
        print(f"{total} passages to embed, {DIMENSIONS} dimensions each")
        done = 0
        while True:
            async with store._pool.connection() as conn:
                rows = await (await conn.execute(
                    "select id, content from jeli.chunks where embedding_backup is null order by id limit %s",
                    (args.batch,),
                )).fetchall()
            if not rows:
                break
            vectors = await local.embed_documents([row["content"] for row in rows])
            async with store._pool.connection() as conn, conn.transaction():
                for row, vector in zip(rows, vectors):
                    await conn.execute(
                        "update jeli.chunks set embedding_backup = %s::vector where id = %s",
                        (list(vector), row["id"]),
                    )
            done += len(rows)
            print(f"  {done}/{total}", flush=True)
        print("done: the memory can now be searched without Google")
    finally:
        await store.close()


run(main())
