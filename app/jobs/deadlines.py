"""Background job: scan new messages and transcripts for deadlines (R14) every hour."""

import asyncio
import logging

from app.answer.deadlines import DeadlineExtractor

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 3600
# The scan shares the Gemini quota with answers: a backlog (a fresh import) is drained a few
# batches an hour rather than in one burst that would leave members without answers.
MAX_BATCHES_PER_RUN = 10


async def extract_periodically(extractor: DeadlineExtractor, interval_seconds: int = INTERVAL_SECONDS) -> None:
    while True:
        try:
            added = await extractor.run(max_batches=MAX_BATCHES_PER_RUN)
            if added:
                log.info("Found %d new deadlines", added)
        except Exception:
            log.exception("Deadline extraction failed, retrying at the next run")
        await asyncio.sleep(interval_seconds)
