"""Grounded answers (R4) that say "I don't know" rather than invent (R6).

1. Retrieve the conversation chunks closest to the question (hybrid search).
2. If even the best one is not similar enough, answer "I don't know" without calling the model.
3. Otherwise give the model the chunks, rebuilt from their messages (minus ignored authors such as
   other bots, with phone numbers masked), and ask for an answer that cites them.
4. Keep only answers that cite at least one real excerpt; show those as sources.
5. If every model is down or out of quota, still point to where the group talked about it.

Call recordings are searched like conversations; their excerpts are timestamped within the call,
and their sources link to the exact moment when the recording is on YouTube.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.answer.citations import (
    author_key,
    display_author,
    format_recording_source,
    format_source,
    format_time,
)
from app.answer.language import TEXTS, detect_language
from app.answer.llm import LLM, LLMUnavailable
from app.answer.prompts import SYSTEM, build_prompt
from app.ingest.transcribe import format_offset
from app.kb.embeddings import Embedder
from app.kb.indexer import RECORDING_PREFIX
from app.kb.search import search
from app.kb.store import Store
from app.models import Recording

log = logging.getLogger(__name__)

RETRIEVED_CHUNKS = 6
SOURCES_SHOWN = 3
FALLBACK_SNIPPET_CHARS = 160


@dataclass(frozen=True)
class Excerpt:
    number: int  # chronological: how the model and the member see it
    relevance_rank: int  # 0 = closest to the question
    chat_label: str
    started_at: datetime
    authors: list[str]
    lines: list[str]  # "Author: text" (phone numbers masked), or "[12:34] Speaker: text" for recordings
    recording: Recording | None = None

    @property
    def offset(self) -> timedelta:
        return self.started_at - self.recording.recorded_at if self.recording else timedelta()

    def for_prompt(self) -> str:
        if self.recording:
            day = f"{self.recording.recorded_at.astimezone(timezone.utc):%d %B %Y}"
            header = f"[{self.number}] Call recording «{self.recording.title}» ({day}), from {format_offset(self.offset)}"
        else:
            header = f"[{self.number}] {self.chat_label} · {format_time(self.started_at)}"
        return header + "\n" + "\n".join(self.lines)

    def source(self) -> str:
        if self.recording:
            return format_recording_source(self.number, self.recording, self.offset, self.authors)
        return format_source(self.number, self.chat_label, self.started_at, self.authors)


class Answerer:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        llm: LLM,
        min_similarity: float,
        ignored_authors: list[str] = (),
        chat_labels: dict[str, str] | None = None,
    ):
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.min_similarity = min_similarity
        self.ignored = {author_key(a) for a in ignored_authors}
        self.chat_labels = chat_labels or {}

    async def answer(self, question: str, asker: str) -> str:
        language = detect_language(question)
        texts = TEXTS[language]
        hits = await search(self.store, self.embedder, question, limit=RETRIEVED_CHUNKS)
        if not hits or max(hit.similarity for hit in hits) < self.min_similarity:
            return texts["dont_know"]

        excerpts = await self._excerpts(hits)
        if not excerpts:
            return texts["dont_know"]

        prompt = build_prompt(question, display_author(asker), [e.for_prompt() for e in excerpts], language)
        try:
            generated = await self.llm.answer(SYSTEM, prompt)
        except LLMUnavailable:
            log.error("No answer model available: sending the closest sources instead")
            return self._fallback(texts, excerpts)

        cited = [e for e in excerpts if e.number in set(generated.sources)]
        if not generated.answered or not cited or not generated.answer.strip():
            return texts["dont_know"]
        sources = "\n".join(e.source() for e in cited[:SOURCES_SHOWN])
        return f"{generated.answer.strip()}\n\n📌 {texts['sources']}\n{sources}"

    async def _excerpts(self, hits) -> list[Excerpt]:
        """Rebuild each chunk from its messages, oldest chunk first, without ignored authors."""
        messages = await self.store.messages_by_ids([id_ for hit in hits for id_ in hit.message_ids])
        recordings = await self.store.recordings(
            sorted({hit.chat_id for hit in hits if hit.chat_id.startswith(RECORDING_PREFIX)})
        )
        by_id = {m.id: m for m in messages}
        rank = {hit.chunk_id: position for position, hit in enumerate(hits)}
        excerpts = []
        for hit in sorted(hits, key=lambda h: h.started_at):
            kept = [
                by_id[id_]
                for id_ in hit.message_ids
                if id_ in by_id and author_key(by_id[id_].author) not in self.ignored
            ]
            if not kept:
                continue
            recording = recordings.get(hit.chat_id)
            if recording:
                lines = [f"[{format_offset(m.sent_at - recording.recorded_at)}] {m.author}: {m.text}" for m in kept]
            else:
                lines = [f"{display_author(m.author)}: {m.text}" for m in kept]
            excerpts.append(
                Excerpt(
                    number=len(excerpts) + 1,
                    relevance_rank=rank[hit.chunk_id],
                    chat_label=self.chat_labels.get(hit.chat_id, hit.chat_id),
                    started_at=kept[0].sent_at,
                    authors=list(dict.fromkeys(m.author for m in kept)),
                    lines=lines,
                    recording=recording,
                )
            )
        return excerpts

    def _fallback(self, texts: dict[str, str], excerpts: list[Excerpt]) -> str:
        lines = []
        for excerpt in sorted(excerpts, key=lambda e: e.relevance_rank)[:SOURCES_SHOWN]:
            snippet = excerpt.lines[0]
            if len(snippet) > FALLBACK_SNIPPET_CHARS:
                snippet = snippet[:FALLBACK_SNIPPET_CHARS].rsplit(" ", 1)[0] + " …"
            lines.append(f"{excerpt.source()}\n   « {snippet} »")
        return f"{texts['fallback']}\n\n" + "\n".join(lines)
