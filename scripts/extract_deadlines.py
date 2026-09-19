"""Scan stored messages and transcripts for deadlines (R14), then list the upcoming ones.

    python -m scripts.extract_deadlines          # scan what was never scanned (e.g. after an import)
    python -m scripts.extract_deadlines --list   # only list the next two weeks

Jeli also does this every hour in the background.
"""

import argparse

from app.answer.deadlines import DeadlineExtractor, Deadlines
from app.answer.llm import LLM
from app.config import get_settings
from app.kb.store import Store
from scripts.common import require, run


async def main(list_only: bool) -> None:
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    await store.open()
    try:
        if not list_only:
            llm = LLM(require(settings.gemini_api_key, "GEMINI_API_KEY"), settings.answer_models)
            extractor = DeadlineExtractor(store, llm, settings.ignored_author_list, settings.chat_label_map)
            print(f"{await extractor.run()} new deadlines found")
        print(await Deadlines(store, settings.chat_label_map).upcoming_reply("en"))
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true")
    run(main(parser.parse_args().list))
