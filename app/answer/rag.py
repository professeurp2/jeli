"""Grounded answers (R4) that say "I don't know" rather than invent (R6).

1. Retrieve the conversation chunks closest to the question — searched with each of its queries
   (the question, its standalone rewording, English and member-language search queries), merged.
   The search on the raw message starts while the message is still being understood.
2. If even the best one is not similar enough, there is no answer in the groups: Jeli explains what it
   knows of the situation instead (awareness.py) — never a bare "I don't know".
3. Otherwise give the model the chunks, rebuilt from their messages (minus ignored authors such as
   other bots, with phone numbers masked), each message numbered, with the community brief as
   background, and ask for an answer that says which messages state it.
4. Keep only answers whose sources are real: a cited message must share words, a number or a date
   with the answer (measured on 21 Sep: attribution by word overlap on whole chunks produced
   "random" references, e.g. a greeting cited under a question about languages, and the sources
   were switched off altogether). One verified source is then shown the WhatsApp way — a reply to
   the source message when it was said in this chat, otherwise a short quote — for factual answers
   only; the rest stays available for "source?".
5. If every model is down or out of quota, still quote where the group talked about it.

Call recordings and documents are searched like conversations; their quotes give the moment in the
call (with a link that plays from there) or the page of the document.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from app.answer.citations import (
    POLL_MARK,
    author_key,
    with_tally,
    display_author,
    ignored_keys,
    is_ignored,
    mention_tag,
    mention_jid,
    best_snippet,
    quote,
    recording_quote,
    short_day,
)
from app.answer.language import TEXTS, detect_language
from app.answer.llm import LLM, LLMUnavailable
from app.answer.persona import background
from app.answer.prompts import DUPLICATE_SYSTEM, SYSTEM, build_prompt
from app.ingest.transcribe import format_offset
from app.kb.embeddings import Embedder
from app.kb.indexer import DOCUMENT_PREFIX, RECORDING_PREFIX, document_page
from app.kb.search import GUARANTEED_SEMANTIC, floor_for, search
from app.kb.store import SearchHit, Store
from app.models import Document, IncomingMessage, Recording, Reply, StoredMessage

log = logging.getLogger(__name__)

RETRIEVED_CHUNKS = 8  # excerpts given to the model
CANDIDATE_CHUNKS = 16  # retrieved, before leaving out ignored authors and near-duplicates
QUOTES_SHOWN = 1  # verified sources shown under a factual answer (sources_mode "one")
# The model found nothing, yet the group discussed something this close: show it rather than a
# flat "I don't know".
NEAR_SIMILARITY = 0.75
NAMES_TTL_SECONDS = 600
CACHE_SECONDS = 600  # the same question asked again (a jury in a row) costs one model call
SOURCE_MODES = ("one", "ask", "off")
CONTENT_WORD = re.compile(r"[^\W\d_]{4,}")
# "[3.1]", "[1.1, 3.6]" the model sometimes leaves in the answer text: ids are for "sources" only.
CITATION_MARK = re.compile(r"\s*\*?\[\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*\]\.?\*?\.?")
TOKEN_NUMBER = re.compile(r"\d[\d:h.,/-]*\d|\d")
STOPWORDS = set(
    """
    that this with from have what when where which there their they them been were will would could
    should about also into over than then some more most very just like only such each other after
    before because dans pour avec sont cette cela nous vous elles leur leurs mais donc tout tous
    toute toutes comme plus aussi entre sans même être avoir fait faire
    """.split()
)


class AlreadyAnswered(BaseModel):
    already_answered: bool
    answer: str
    sources: list[str] = []


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w+", text.lower()) if len(w) > 3}


def _content_words(text: str) -> set[str]:
    return {w.lower() for w in CONTENT_WORD.findall(text)} - STOPWORDS


def supports(source: str, answer: str) -> bool:
    """A cited message really states something of the answer: they share a content word (names,
    topics — often the same across languages) or a number, time or date."""
    shared_words = _content_words(source) & _content_words(answer)
    shared_numbers = set(TOKEN_NUMBER.findall(source)) & set(TOKEN_NUMBER.findall(answer))
    return bool(shared_words) or bool(shared_numbers)


def from_brief(brief: str, question: str, answer: str) -> bool:
    """An answer said to come from the brief really does: what it adds to the question (its
    content words not already in the question) is in the brief. Measured: with the brief
    mentioning "METI Japan", a lite model answered "Tokyo is the capital of Japan" as background."""
    added = _content_words(answer) - _content_words(question)
    if not added:
        return False
    known = _content_words(brief)
    return len(added & known) / len(added) >= 0.6


def _display_label(raw: str) -> str:
    """Return the label for display; '' when it looks like a raw filename (underscores, no spaces)."""
    if not raw or ("_" in raw and " " not in raw):
        return ""
    return raw


def clean_answer(text: str) -> str:
    """The answer without citation ids the model wrote into it ("… as stated [3.1]." → "… as stated.")."""
    cleaned = CITATION_MARK.sub(lambda m: "." if "." in m.group(0).split("]")[-1] else "", text)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", cleaned).strip()


def clean_title(title: str) -> str:
    """A document's title as captions give it ("📎 *Hackathon Guidelines*") shown plainly."""
    return " ".join(re.sub(r"[*_~`]", "", title).replace("📎", "").split())


def parse_source(value) -> tuple[int, int | None] | None:
    """"3.2" → (3, 2); "3" → (3, None); anything else → None."""
    match = re.fullmatch(r"\s*\[?(\d+)(?:\.(\d+))?\]?\s*", str(value))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)) if match.group(2) else None


@dataclass(frozen=True)
class Excerpt:
    number: int  # chronological: how the model sees it
    relevance_rank: int  # 0 = closest to the question
    chat_id: str
    chat_label: str
    started_at: datetime
    messages: tuple[StoredMessage, ...]
    names: dict  # phone number digits → WhatsApp name
    recording: Recording | None = None
    document: Document | None = None
    organisers: frozenset = frozenset()  # keys of the organisers: their messages are announcements
    tallies: dict = None  # poll message id → votes per option

    def __post_init__(self):
        if self.tallies is None:
            object.__setattr__(self, "tallies", {})

    def by_organiser(self, message: StoredMessage) -> bool:
        return is_ignored(message, set(self.organisers))

    @property
    def has_announcement(self) -> bool:
        return any(self.by_organiser(m) for m in self.messages)

    @property
    def authors(self) -> list[str]:
        return list(dict.fromkeys(m.author for m in self.messages))

    def name(self, message: StoredMessage) -> str:
        return self.names.get(author_key(message.author)) or display_author(message.author)

    def _line(self, message: StoredMessage, position: int) -> str:
        tag = f"[{self.number}.{position}]"
        if self.recording:
            return f"{tag} [{format_offset(message.sent_at - self.recording.recorded_at)}] {message.author}: {message.text}"
        if self.document:
            return f"{tag} [page {document_page(message.sent_at, self.document.shared_at)}] {message.text}"
        role = " (organiser)" if self.by_organiser(message) else ""
        return f"{tag} [{message.sent_at:%d %b %H:%M}] {self.name(message)}{role}: {with_tally(message.text, self.tallies.get(message.id))}"

    @property
    def lines(self) -> list[str]:
        return [self._line(m, position) for position, m in enumerate(self.messages, 1)]

    def for_prompt(self) -> str:
        if self.recording:
            day = f"{self.recording.recorded_at:%d %B %Y}"
            header = f"[{self.number}] Call recording «{self.recording.title}» ({day})"
        elif self.document:
            header = f"[{self.number}] Document «{self.document.title}», shared by {display_author(self.document.shared_by)}"
        else:
            label = self.chat_label or self.chat_id.split("@")[0]
            header = f"[{self.number}] {label} · {self.started_at:%d %B %Y}"
        return header + "\n" + "\n".join(self.lines)

    def message_at(self, position: int | None, words: set[str]) -> StoredMessage:
        """The message a source id points at; without a position, the one that says most of the answer."""
        if position is not None and 1 <= position <= len(self.messages):
            return self.messages[position - 1]
        return self.best_message(words)

    def best_message(self, words: set[str]) -> StoredMessage:
        """The message that says what the answer says: most words in common."""
        return max(self.messages, key=lambda m: len(words & _words(m.text)))

    def place_of(self, message: StoredMessage) -> tuple:
        if self.document:
            return (self.chat_id, document_page(message.sent_at, self.document.shared_at))
        return (message.id,)

    def place(self, words: set[str]) -> tuple:
        """Where the quote comes from: two quotes from the same place are one."""
        return self.place_of(self.best_message(words))

    def quote_message(self, message: StoredMessage, words: set[str], language: str = "en") -> str:
        """The block shown under an answer, written in the member's language.

        A French reply ending in "Thu 17 Sep, at 12:30 · organiser" is the mixing a member
        reported on 24 September: the answer was theirs, its date and its labels were not.
        """
        if self.recording:
            return recording_quote(
                self.recording, message.sent_at - self.recording.recorded_at, message.author,
                message.text, language=language,
            )
        if self.document:
            page = document_page(message.sent_at, self.document.shared_at)
            return quote(f"📄 *{clean_title(self.document.title)}*, page {page}", best_snippet(message.text, words))
        role = f" · {TEXTS[language]['source_organiser']}" if self.by_organiser(message) else ""
        clean = _display_label(self.chat_label)
        label = f" · {clean}" if clean else ""
        return quote(
            f"*{self.name(message)}*{role}{label}, {short_day(message.sent_at, language)}",
            best_snippet(message.text, words),
        )

    def quote(self, words: set[str], language: str = "en") -> str:
        return self.quote_message(self.best_message(words), words, language)


def distinct(excerpts: list[Excerpt], words: set[str]) -> list[Excerpt]:
    """Excerpts that quote different places (a document's page is quoted once)."""
    seen, kept = set(), []
    for excerpt in excerpts:
        place = excerpt.place(words)
        if place not in seen:
            seen.add(place)
            kept.append(excerpt)
    return kept


def merge(results: list[list[SearchHit]], limit: int) -> list[SearchHit]:
    """Hits of several queries in one list: a chunk found by several queries ranks higher (fused
    ranks), and keeps its best similarity. Near-duplicate chunks (mostly the same messages — a
    history imported twice) count once."""
    scores: dict[int, float] = {}
    best: dict[int, SearchHit] = {}
    for hits in results:
        for rank, hit in enumerate(hits):
            scores[hit.chunk_id] = scores.get(hit.chunk_id, 0) + 1 / (60 + rank)
            if hit.chunk_id not in best or hit.similarity > best[hit.chunk_id].similarity:
                best[hit.chunk_id] = hit
    ordered = sorted(best.values(), key=lambda hit: scores[hit.chunk_id], reverse=True)
    ordered = _without_near_duplicates(ordered)
    closest = sorted(ordered, key=lambda hit: hit.similarity, reverse=True)[:GUARANTEED_SEMANTIC]
    chosen = (closest + [hit for hit in ordered if hit not in closest])[:limit]
    return [hit for hit in ordered if hit in chosen]


def _without_near_duplicates(hits: list[SearchHit]) -> list[SearchHit]:
    kept: list[SearchHit] = []
    seen: list[set[str]] = []
    texts: set[str] = set()
    for hit in hits:
        ids = set(hit.message_ids)
        # The content without its header (the header names the chat, which differs between two
        # imports of the same conversation).
        body = " ".join(hit.content.split("\n", 1)[-1].split())[:400] if hit.content else ""
        duplicate = any(
            (ids and other and len(ids & other) / min(len(ids), len(other)) > 0.5) for other in seen
        ) or (bool(body) and body in texts)
        if not duplicate:
            kept.append(hit)
            seen.append(ids)
            if body:
                texts.add(body)
    return kept


class Answerer:
    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        llm: LLM,
        min_similarity: float,
        ignored_authors: list[str] = (),
        chat_labels: dict[str, str] | None = None,
        clock=time.monotonic,
    ):
        self.store = store
        self.embedder = embedder
        self.llm = llm
        self.min_similarity = min_similarity
        self.ignored = ignored_keys(ignored_authors)
        self.chat_labels = chat_labels or {}
        self._names: tuple[float, dict[str, str]] | None = None
        # Explains why there is no answer from what Jeli knows of its own state (awareness.py).
        self.explainer = None
        # Keys of the organisers (set from the settings): their announcements rank first.
        self.organisers: set[str] = set()
        self.known_names: dict[str, str] = {}  # number → name, given by the team
        self.brief = ""  # the community brief (app/answer/brief.py), background for every answer
        # "one": one verified source under factual answers; "ask": kept for "source?" only; "off".
        self.sources_mode = "one"
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, str, list, bool]] = {}

    async def _search(self, queries: list[str], prefetched: list[SearchHit] | None = None) -> list[SearchHit]:
        queries = list(dict.fromkeys(q for q in queries if q.strip()))[:4]
        results = await asyncio.gather(*(search(self.store, self.embedder, q, limit=CANDIDATE_CHUNKS) for q in queries))
        results = list(results) + ([prefetched] if prefetched else [])
        return merge(results, CANDIDATE_CHUNKS)

    async def prefetch(self, text: str) -> list[SearchHit]:
        """The search on the raw message, started while the message is still being understood."""
        try:
            return await search(self.store, self.embedder, text, limit=CANDIDATE_CHUNKS)
        except Exception:
            log.exception("Prefetch failed")
            return []

    def _cache_key(self, question: str, chat_id: str | None) -> tuple[str, str]:
        return " ".join(re.findall(r"\w+", question.lower())), chat_id or ""

    async def answer(
        self,
        question: str,
        asker: str,
        chat_id: str | None = None,
        asker_id: str | None = None,
        queries: list[str] = (),
        language: str | None = None,
        member: str = "",
        prefetched: list[SearchHit] | None = None,
        language_name: str = "",
    ) -> str:
        language = language or detect_language(question)
        texts = TEXTS[language]
        key = self._cache_key(question, chat_id)
        cached = self._cache.get(key)
        if cached and self._clock() - cached[0] < CACHE_SECONDS:
            _, answer, cited, from_background = cached
            return self._reply(answer, cited, chat_id, asker_id, factual=not from_background, language=language)

        hits = await self._search([question, *queries], prefetched)
        state = await self._state()
        context = background(self.brief, state, member)
        if not hits or max(hit.similarity for hit in hits) < floor_for(hits, self.min_similarity):
            excerpts = []
        else:
            excerpts = await self._excerpts(hits)
        if not excerpts and not self.brief:
            return await self._no_answer(question, language, member=member)

        prompt = build_prompt(
            question, display_author(asker), [e.for_prompt() for e in excerpts], language, context=context, language_name=language_name
        )
        try:
            generated = await self.llm.answer(SYSTEM, prompt)
        except LLMUnavailable:
            log.error("No answer model available: quoting the closest sources instead")
            if excerpts:
                return self._quotes(texts["fallback"], excerpts, question, shown=2, language=language)
            return await self._no_answer(question, language, member=member)

        answer = clean_answer(generated.answer)
        cited = self._verified(generated.sources, excerpts, answer)
        background_only = not cited and generated.from_background and from_brief(self.brief, question, answer)
        if not generated.answered or not answer or (not cited and not background_only):
            near = bool(hits) and max(hit.similarity for hit in hits) >= NEAR_SIMILARITY
            return await self._no_answer(question, language, excerpts if near else [], member=member)
        self._cache[key] = (self._clock(), answer, cited, background_only)
        return self._reply(answer, cited, chat_id, asker_id, factual=bool(cited), language=language)

    async def _state(self) -> str:
        state = getattr(self, "state", None)
        if state is None:
            return ""
        try:
            return await state()
        except Exception:
            log.exception("Could not read Jeli's state")
            return ""

    def _verified(self, sources, excerpts: list[Excerpt], answer: str) -> list[tuple[Excerpt, StoredMessage]]:
        """The (excerpt, message) pairs the model cited that really state something of the answer."""
        by_number = {e.number: e for e in excerpts}
        words = _words(answer)
        cited: list[tuple[Excerpt, StoredMessage]] = []
        seen: set[tuple] = set()
        for value in sources:
            parsed = parse_source(value)
            if not parsed or parsed[0] not in by_number:
                continue
            excerpt = by_number[parsed[0]]
            message = excerpt.message_at(parsed[1], words)
            place = excerpt.place_of(message)
            if place in seen or not supports(message.text, answer):
                continue
            seen.add(place)
            cited.append((excerpt, message))
        return cited

    async def _no_answer(self, question: str, language: str, near: list[Excerpt] = (), member: str = "") -> str:
        """Not "I don't know" alone: what Jeli knows of the situation, and the closest discussions."""
        texts = TEXTS[language]
        if self.explainer is None:
            return self._quotes(texts["dont_know_near"], list(near), question, language=language) if near else texts["dont_know"]
        words = _words(question)
        quotes = "\n\n".join(e.quote(words, language) for e in distinct(sorted(near, key=lambda e: e.relevance_rank), words)[:QUOTES_SHOWN])
        return Reply(await self.explainer(question, language, quotes, member=member), unanswered=True)

    async def already_answered(
        self, question: str, min_similarity: float, chat_id: str | None = None, asker_id: str | None = None
    ) -> str | None:
        """For a question asked in the group (not to Jeli): the group's earlier answer, or None.

        Jeli speaks uninvited only when sure: a stricter similarity gate, a model asked to confirm
        that an excerpt answers this very question, and silence on any doubt or failure.
        """
        language = detect_language(question)
        hits = await search(self.store, self.embedder, question, limit=CANDIDATE_CHUNKS)
        if not hits or max(hit.similarity for hit in hits) < floor_for(hits, min_similarity):
            return None
        excerpts = await self._excerpts(hits)
        if not excerpts:
            return None
        prompt = build_prompt(question, "a member", [e.for_prompt() for e in excerpts], language, context=background(self.brief))
        try:
            generated = await self.llm.generate(prompt, AlreadyAnswered, system=DUPLICATE_SYSTEM)
        except LLMUnavailable:
            return None
        answer = clean_answer(generated.answer)
        cited = self._verified(generated.sources, excerpts, answer)
        if not generated.already_answered or not cited or not answer:
            return None
        return self._reply(answer, cited, chat_id, asker_id, lead=TEXTS[language]["already_covered"] + " ", factual=True, language=language)

    def is_ignored(self, message: StoredMessage | IncomingMessage) -> bool:
        return is_ignored(message, self.ignored)

    def _reply(
        self,
        answer: str,
        cited: list[tuple[Excerpt, StoredMessage]],
        chat_id: str | None,
        asker_id: str | None,
        lead: str = "",
        factual: bool = True,
        language: str = "en",
    ) -> Reply:
        """The answer with its sources, as WhatsApp does it — an organiser's announcement first."""
        words = _words(answer)
        ordered = sorted(cited, key=lambda pair: (not pair[0].by_organiser(pair[1]), pair[0].relevance_rank))
        quotes = [excerpt.quote_message(message, words, language) for excerpt, message in ordered]
        show = self.sources_mode == "one" and factual
        if ordered and show:
            first, source = ordered[0]
            if chat_id and source.chat_id == chat_id and source.source == "whatsapp_live":
                # Said in this very chat: reply to that message, which WhatsApp quotes above the
                # answer (a tap jumps to it), and mention the member who asked so they get it.
                mention = f"{mention_tag(asker_id)} " if asker_id and "@" in asker_id else ""
                return Reply(
                    mention + lead + answer,
                    reply_to=source.id,
                    quoted=(first.name(source), source.text),
                    mentions=[mention_jid(asker_id)] if mention else [],
                    cited=quotes,
                )
            return Reply(lead + answer + "".join(f"\n\n{q}" for q in quotes[:QUOTES_SHOWN]), cited=quotes)
        return Reply(lead + answer, cited=quotes if self.sources_mode != "off" else [])

    def _quotes(self, header: str, excerpts: list[Excerpt], question: str, shown: int = QUOTES_SHOWN,
                language: str = "en") -> str:
        words = _words(question)
        best = distinct(sorted(excerpts, key=lambda e: e.relevance_rank), words)[:shown]
        return header + "".join(f"\n\n{e.quote(words, language)}" for e in best)

    async def _member_names(self) -> dict[str, str]:
        if self._names and time.monotonic() - self._names[0] < NAMES_TTL_SECONDS:
            return self._names[1]
        lookup = getattr(self.store, "member_names", None)
        names = {**(await lookup() if lookup else {}), **self.known_names}
        self._names = (time.monotonic(), names)
        return names

    async def _excerpts(self, hits, keep: int = RETRIEVED_CHUNKS) -> list[Excerpt]:
        """Rebuild the best `keep` chunks from their messages, without ignored authors, oldest first.

        Hits are over-fetched: measured, half of the chunks retrieved for a hackathon question were
        entirely another bot's messages, which left no excerpt with the answer once filtered out.
        """
        messages = await self.store.messages_by_ids([id_ for hit in hits for id_ in hit.message_ids])
        recordings = await self.store.recordings(
            sorted({hit.chat_id for hit in hits if hit.chat_id.startswith(RECORDING_PREFIX)})
        )
        document_ids = sorted({hit.chat_id for hit in hits if hit.chat_id.startswith(DOCUMENT_PREFIX)})
        documents = await self.store.documents(document_ids) if document_ids else {}
        names = await self._member_names()
        polls = [m.id for m in messages if m.text.startswith(POLL_MARK)]
        tallies = await self.store.poll_tallies(polls) if polls and hasattr(self.store, "poll_tallies") else {}
        by_id = {m.id: m for m in messages}
        usable = []  # (fused rank, hit, messages kept), in fused order
        for position, hit in enumerate(hits):
            kept = [by_id[id_] for id_ in hit.message_ids if id_ in by_id and not self.is_ignored(by_id[id_])]
            if kept:
                usable.append((position, hit, kept))
        # As in search: the semantically closest are kept first, then the best of the fused order —
        # where an organiser's announcement goes before members' talk about it.
        closest = sorted(usable, key=lambda item: item[1].similarity, reverse=True)[:GUARANTEED_SEMANTIC]
        announced = [item for item in usable if item not in closest and any(is_ignored(m, self.organisers) for m in item[2])]
        chosen = (closest + announced + [item for item in usable if item not in closest and item not in announced])[:keep]

        excerpts = []
        for position, hit, kept in sorted(chosen, key=lambda item: item[1].started_at):
            document = documents.get(hit.chat_id)
            excerpts.append(
                Excerpt(
                    number=len(excerpts) + 1,
                    relevance_rank=position,
                    chat_id=hit.chat_id,
                    chat_label=document.title if document else self.chat_labels.get(hit.chat_id, ""),
                    started_at=kept[0].sent_at,
                    messages=tuple(kept),
                    names=names,
                    recording=recordings.get(hit.chat_id),
                    document=document,
                    organisers=frozenset(self.organisers),
                    tallies=tallies,
                )
            )
        return excerpts

    async def where_discussed(self, topic: str) -> str:
        """R11, /search: where the group talked about a topic — quotes, no model call, so it keeps
        working when every model is out of quota."""
        language = detect_language(topic)
        texts = TEXTS[language]
        hits = await search(self.store, self.embedder, topic, limit=CANDIDATE_CHUNKS)
        if not hits or max(hit.similarity for hit in hits) < floor_for(hits, self.min_similarity):
            return texts["search_nothing"]
        excerpts = await self._excerpts(hits)
        return self._quotes(texts["search_header"], excerpts, topic, shown=3, language=language) if excerpts else texts["search_nothing"]
