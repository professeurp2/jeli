"""Write the recaps of imported call recordings (R9): summary, decisions, action items, key moments.

    python -m scripts.recap_recording                  # every recording without an English recap
    python -m scripts.recap_recording --language fr    # French recaps (otherwise made on first request)
    python -m scripts.recap_recording --redo           # rewrite existing ones too

New imports get their English recap automatically (scripts.import_recording).
"""

import argparse
import asyncio

from app.answer.llm import LLM
from app.answer.recaps import Recaps, format_recap
from app.config import get_settings
from app.kb.store import Store
from scripts.common import require, run


async def main(language: str, redo: bool, show: bool) -> None:
    settings = get_settings()
    store = Store(require(settings.database_url, "DATABASE_URL"))
    recaps = Recaps(store, LLM(require(settings.gemini_api_key, "GEMINI_API_KEY"), settings.answer_models))
    await store.open()
    try:
        for recording in await store.all_recordings():
            if (recording.recap or {}).get(language) and not redo:
                print(f"= {recording.title}: already has a recap")
                continue
            data = await recaps.generate(recording, language)
            print(f"+ {recording.title}: recap written")
            if show:
                print("\n" + format_recap(recording, data, language) + "\n")
            await asyncio.sleep(5)  # spread the calls over the free tier's per-minute limits
    finally:
        await store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--language", default="en", choices=["en", "fr"])
    parser.add_argument("--redo", action="store_true")
    parser.add_argument("--show", action="store_true", help="print the recaps")
    args = parser.parse_args()
    run(main(args.language, args.redo, args.show))
