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
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable
from app.answer.prompts import DUPLICATE_SYSTEM, SYSTEM, build_prompt
from app.ingest.transcribe import format_offset
from app.kb.embeddings import Embedder
from app.kb.indexer import RECORDING_PREFIX
from app.kb.search import GUARANTEED_SEMANTIC, search
from app.kb.store import Store
from app.models import IncomingMessage, Recording, StoredMessage

log = logging.getLogger(__name__)

RETRIEVED_CHUNKS = 6  # excerpts given to the model
CANDIDATE_CHUNKS = 12  # retrieved, before leaving out ignored authors
SOURCES_SHOWN = 3
FALLBACK_SNIPPET_CHARS = 160


class AlreadyAnswered(BaseModel):
    already_answered: bool
    answer: str
    sources: list[int]


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
        hits = await search(self.store, self.embedder, question, limit=CANDIDATE_CHUNKS)
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
        return self._with_sources(generated.answer, cited, texts)

    async def already_answered(self, question: str, min_similarity: float) -> str | None:
        """For a question asked in the group (not to Jeli): the group's earlier answer, or None.

        Jeli speaks uninvited only when sure: a stricter similarity gate, a model asked to confirm
        that an excerpt answers this very question, and silence on any doubt or failure.
        """
        language = detect_language(question)
        hits = await search(self.store, self.embedder, question, limit=CANDIDATE_CHUNKS)
        if not hits or max(hit.similarity for hit in hits) < min_similarity:
            return None
        excerpts = await self._excerpts(hits)
        if not excerpts:
            return None
        prompt = build_prompt(question, "a member", [e.for_prompt() for e in excerpts], language)
        try:
            generated = await self.llm.generate(prompt, AlreadyAnswered, system=DUPLICATE_SYSTEM)
        except LLMUnavailable:
            return None
        cited = [e for e in excerpts if e.number in set(generated.sources)]
        if not generated.already_answered or not cited or not generated.answer.strip():
            return None
        texts = TEXTS[language]
        return self._with_sources(f"{texts['already_covered']}\n{generated.answer.strip()}", cited, texts)

    def is_ignored(self, message: StoredMessage | IncomingMessage) -> bool:
        """By display name or phone number, and by WhatsApp id: live messages carry a display name,
        not the phone number that exports show."""
        keys = {author_key(message.author)}
        if message.author_id:
            keys.add(author_key(message.author_id.split("@")[0]))
        return bool(keys & self.ignored)

    @staticmethod
    def _with_sources(answer: str, cited: list[Excerpt], texts: dict[str, str]) -> str:
        sources = "\n".join(e.source() for e in cited[:SOURCES_SHOWN])
        return f"{answer.strip()}\n\n📌 {texts['sources']}\n{sources}"

    async def _excerpts(self, hits, keep: int = RETRIEVED_CHUNKS) -> list[Excerpt]:
        """Rebuild the best `keep` chunks from their messages, without ignored authors, oldest first.

        Hits are over-fetched: measured, half of the chunks retrieved for a hackathon question were
        entirely another bot's messages, which left no excerpt with the answer once filtered out.
        """
        messages = await self.store.messages_by_ids([id_ for hit in hits for id_ in hit.message_ids])
        recordings = await self.store.recordings(
            sorted({hit.chat_id for hit in hits if hit.chat_id.startswith(RECORDING_PREFIX)})
        )
        by_id = {m.id: m for m in messages}
        usable = []  # (fused rank, hit, messages kept), in fused order
        for position, hit in enumerate(hits):
            kept = [by_id[id_] for id_ in hit.message_ids if id_ in by_id and not self.is_ignored(by_id[id_])]
            if kept:
                usable.append((position, hit, kept))
        # As in search: the semantically closest are kept first, then the best of the fused order.
        closest = sorted(usable, key=lambda item: item[1].similarity, reverse=True)[:GUARANTEED_SEMANTIC]
        chosen = (closest + [item for item in usable if item not in closest])[:keep]

        excerpts = []
        for position, hit, kept in sorted(chosen, key=lambda item: item[1].started_at):
            recording = recordings.get(hit.chat_id)
            if recording:
                lines = [f"[{format_offset(m.sent_at - recording.recorded_at)}] {m.author}: {m.text}" for m in kept]
            else:
                lines = [f"{display_author(m.author)}: {m.text}" for m in kept]
            excerpts.append(
                Excerpt(
                    number=len(excerpts) + 1,
                    relevance_rank=position,
                    chat_label=self.chat_labels.get(hit.chat_id, hit.chat_id),
                    started_at=kept[0].sent_at,
                    authors=list(dict.fromkeys(m.author for m in kept)),
                    lines=lines,
                    recording=recording,
                )
            )
        return excerpts

    async def where_discussed(self, topic: str) -> str:
        """R11, /search: where the group talked about a topic — sources and snippets, no model call,
        so it keeps working when every model is out of quota."""
        texts = TEXTS[detect_language(topic)]
        hits = await search(self.store, self.embedder, topic, limit=CANDIDATE_CHUNKS)
        if not hits or max(hit.similarity for hit in hits) < self.min_similarity:
            return texts["search_nothing"]
        excerpts = await self._excerpts(hits)
        return self._fallback(texts, excerpts, header="search_header") if excerpts else texts["search_nothing"]

    def _fallback(self, texts: dict[str, str], excerpts: list[Excerpt], header: str = "fallback") -> str:
        lines = []
        for excerpt in sorted(excerpts, key=lambda e: e.relevance_rank)[:SOURCES_SHOWN]:
            snippet = excerpt.lines[0]
            if len(snippet) > FALLBACK_SNIPPET_CHARS:
                snippet = snippet[:FALLBACK_SNIPPET_CHARS].rsplit(" ", 1)[0] + " …"
            lines.append(f"{excerpt.source()}\n   « {snippet} »")
        return f"{texts[header]}\n\n" + "\n".join(lines)
