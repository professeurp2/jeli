"""Ask Jeli a question from the command line, through the same pipeline as WhatsApp.

    python -m scripts.ask "When is the bootcamp?"
"""

import argparse
import time

from app.answer.llm import LLM
from app.answer.rag import Answerer
from app.config import Settings, get_settings
from app.kb.embeddings import Embedder
from app.kb.store import Store
from scripts.common import require, run


def build_answerer(settings: Settings, store: Store, llm: LLM | None = None) -> Answerer:
    keys = settings.api_key_list
    require(keys[0] if keys else "", "GEMINI_API_KEY")
    return Answerer(
        store,
        Embedder(keys),
        llm or LLM(keys, settings.answer_models),
        min_similarity=settings.answer_min_similarity,
        ignored_authors=settings.ignored_author_list,
        chat_labels=settings.chat_label_map,
    )


async def main(question: str) -> None:
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    await store.open()
    try:
        started = time.monotonic()
        reply = await build_answerer(settings, store).answer(question, asker="you")
        print(f"{reply}\n\n({time.monotonic() - started:.1f} s)")
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    run(main(parser.parse_args().question))
