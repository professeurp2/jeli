"""Search Jeli's knowledge base from the command line.

    python -m scripts.search "When is the bootcamp?"
"""

import argparse
import textwrap

from app.config import get_settings
from app.kb.embeddings import Embedder
from app.kb.search import search
from app.kb.store import Store
from scripts.common import require, run


async def main(question: str, limit: int) -> None:
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    embedder = Embedder(require(settings.gemini_api_key, "GEMINI_API_KEY"))
    await store.open()
    try:
        hits = await search(store, embedder, question, limit=limit)
    finally:
        await store.close()
    if not hits:
        print("Nothing found.")
    for rank, hit in enumerate(hits, 1):
        print(f"\n#{rank}  similarity {hit.similarity:.2f}  {hit.started_at:%Y-%m-%d %H:%M} → {hit.ended_at:%H:%M} UTC")
        print(textwrap.indent(textwrap.shorten(hit.content.replace("\n", " ¶ "), 400), "    "))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    run(main(args.question, args.limit))
