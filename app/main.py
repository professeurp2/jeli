import asyncio
import contextlib
import dataclasses
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import telegram, whatsapp_waha
from app.answer.brief import Brief
from app.answer.catchup import Catchup
from app.answer.illustrator import IMAGE_MODELS
from app.answer.models_check import report_models
from app.answer.voice import GEMINI_TTS_MODELS
from app.kb.embeddings import MODEL as EMBEDDING_MODEL
from app.answer.deadlines import DeadlineExtractor, Deadlines
from app.answer.awareness import Awareness
from app.answer.documents import Documents
from app.answer.groq import Groq
from app.answer.llm import LLM
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.answer.responder import Responder
from app.answer.understand import Understander
from app.answer.emotion import Emotions
from app.answer.reminders import Reminders
from app.answer.voice import Voice
from app.answer.citations import ignored_keys, is_ignored
from app.config import get_settings
from app.control.apply import apply
from app.control.guard import Guard
from app.control.runtime import Runtime
from app.control.setup import build_activities
from app.ingest.live import LiveIngestor
from app.ingest.sessions import Sessions
from app.kb.embeddings import Embedder
from app.kb.local_embeddings import LocalEmbedder
from app.kb.store import Store
from app.models import IncomingMessage
from app.system_certificates import use_system_certificates
from app.web import pages
from app.web.auth import Auth


@asynccontextmanager
async def lifespan(app: FastAPI):
    use_system_certificates()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO level, and Telegram API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    state = app.state

    # Knowledge base: messages are stored as they arrive, and indexed in the background.
    store = Store(settings.database_url) if settings.database_url else None
    if store:
        await store.open()
    state.store = store
    # The memory's spare sense: the local model, loaded in the background (the first start
    # downloads it). No key, no quota, no network — Groq has no embedding model of its own.
    state.local_embedder = LocalEmbedder() if settings.local_embeddings else None
    state.embedder = Embedder(settings.api_key_list, backup=state.local_embedder) if settings.api_key_list else None
    if state.local_embedder is not None:
        asyncio.create_task(state.local_embedder.load())

    # The team's settings from the dashboard, over the environment's.
    runtime = Runtime(settings, store)
    await runtime.load()
    state.runtime = runtime
    state.auth = Auth(settings.dashboard_accounts, settings.dashboard_secret, store)

    # Grounded answers need the knowledge base and a Gemini key; without them Jeli says it isn't ready.
    state.llm = state.answerer = state.catchup = state.recaps = state.deadlines = state.extractor = None
    state.documents = state.sessions = state.awareness = state.brief = None
    state.missing_models = []
    # Groq, the spare engine: when every Gemini model refuses at once, it answers instead of Jeli
    # telling the member to come back later. Shared by every tier (with_models copies it).
    backup = Groq(settings.groq_api_key, settings.groq_model_list)
    # Jeli thinks with Gemini, with the spare engine, or both. Only the memory search — questions
    # about what was said in the groups — needs Google's embeddings: without them Jeli still
    # catches up, lists deadlines, sets reminders and talks, on whichever engine answers.
    if store and (state.embedder or backup.available):
        state.llm = LLM(settings.api_key_list, settings.answer_models, backup=backup)
        # The answers members read (questions, catch-ups, session recaps and questions, the brief)
        # start with the best model; everything else uses the light models, 25 times more quota.
        state.light_llm = state.llm.with_models(settings.light_model_list)
        if state.embedder:
            state.answerer = Answerer(store, state.embedder, state.llm, min_similarity=runtime["answer_min_similarity"])
        state.deadlines = Deadlines(store, llm=state.light_llm)
        state.catchup = Catchup(store, state.llm, deadlines=state.deadlines)
        state.recaps = Recaps(store, state.llm)
        state.extractor = DeadlineExtractor(store, state.light_llm)
        state.documents = Documents(store, state.light_llm)

        async def learn_now() -> None:
            memory = state.activities.get("memory")
            if memory:
                memory.run_now("Jeli")

        state.sessions = Sessions(
            store, state.llm.with_models(settings.transcription_model_list), state.recaps, learn_now, reader=state.light_llm
        )
        state.awareness = Awareness(store, state.light_llm, state.sessions)
        if state.answerer:
            state.answerer.explainer = state.awareness.explain
            state.answerer.state = state.awareness.state
        # The community brief: Jeli's general knowledge, background for every prompt.
        state.brief = Brief(store, state.llm)
        await state.brief.load()

        async def check_models() -> None:
            wanted = list(dict.fromkeys(
                [*settings.answer_models, *settings.light_model_list, *settings.transcription_model_list, *IMAGE_MODELS,
                 *GEMINI_TTS_MODELS, EMBEDDING_MODEL]
            ))
            state.missing_models = await report_models(state.llm.client, wanted)

        if state.llm.client is not None:  # nothing to check without a Gemini key
            asyncio.create_task(check_models())
        if backup.available:
            asyncio.create_task(backup.check())
    state.light_llm = getattr(state, "light_llm", None) if state.llm else None
    state.understander = Understander(state.light_llm)
    if state.awareness is not None:
        state.understander.state = state.awareness.state
    state.responder = Responder(
        state.answerer,
        state.catchup,
        recaps=state.recaps,
        deadlines=state.deadlines,
        record=store.record_event if store else None,
        understander=state.understander,
        documents=state.documents,
        store=store,
    )
    state.guard = Guard(record=store.record_incident if store else None)
    state.reminders = Reminders(store, state.light_llm, deadlines=state.deadlines) if store and state.llm else None
    state.responder.reminders = state.reminders
    state.voice = Voice(state.light_llm) if state.llm else None  # voice notes, heard and spoken
    if state.voice is not None:
        state.voice.store = store  # today's count of natural voice notes survives a restart

    async def respond(message: IncomingMessage) -> str | None:
        """What the channels call: nothing at all while the team has paused Jeli. A group message
        continuing a conversation with Jeli is for Jeli, without repeating its name."""
        if runtime.paused:
            return None
        if not message.addressed_to_bot and state.responder.is_follow_up(message):
            message = dataclasses.replace(message, addressed_to_bot=True)
        return await state.responder.respond(message)

    live = LiveIngestor(store) if store else None

    async def is_organiser(message: IncomingMessage) -> bool | None:
        """Listed by the team, or an admin of the group; None when nobody is known as one."""
        listed = ignored_keys(runtime["organisers"])
        if is_ignored(message, listed):
            return True
        admins = await state.whatsapp.group_admins(message.chat_id) if state.whatsapp else None
        if admins:
            return bool({(message.author_id or "").split("@")[0].split(":")[0]} & admins)
        return None if not listed else False

    async def ingest(message: IncomingMessage) -> None:
        """Remember every group message; a session's recording an organiser shares is added too."""
        await live.ingest(message)
        if not (state.sessions and runtime["auto_sessions"]) or message.is_private or "http" not in message.text:
            return
        try:
            organiser = await is_organiser(message)
            if organiser:
                await state.sessions.from_organiser(message)
            elif organiser is None:
                state.sessions.from_group(message)  # nobody known as organiser: the words alone
        except ValueError:
            pass
        except Exception:
            logging.getLogger(__name__).exception("Could not check a shared link for a recording")

    # Each adapter runs when its environment variables are set; WhatsApp is the target channel.
    state.whatsapp = whatsapp_waha.start(settings, respond, ingest=ingest if store else None)
    if state.whatsapp:
        state.whatsapp.guard = state.guard
        state.whatsapp.follow_up = state.responder.is_follow_up
        state.whatsapp.in_conversation = state.responder.in_conversation
        state.whatsapp.warm = state.responder.warm
        state.whatsapp.voice = state.voice
        state.whatsapp.emotions = Emotions(state.light_llm) if state.llm else None  # reactions that fit the feeling
        if state.documents:
            state.whatsapp.on_document = state.documents.add
        if store:
            state.whatsapp.on_vote = store.save_poll_vote
            state.whatsapp.on_feedback = store.record_feedback
        await state.whatsapp.sync_status()
    state.telegram = await telegram.start(settings, respond)

    # Background activities, then the team's settings applied to everything, now and after each change.
    state.activities = {}
    state.activities = build_activities(state, settings, runtime)
    apply(state, runtime)
    runtime.listeners.append(lambda changed: apply(state, runtime))
    for activity in state.activities.values():
        activity.start()
    yield
    for activity in state.activities.values():
        await activity.close()
    if state.whatsapp:
        await whatsapp_waha.stop(state.whatsapp)
    if state.telegram:
        await telegram.stop(state.telegram)
    if store:
        with contextlib.suppress(Exception):
            await store.close()


# No public API documentation: the only public pages are /health and the dashboard's sign-in.
app = FastAPI(title="Jeli", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(whatsapp_waha.router)
app.include_router(telegram.router)
app.include_router(pages.router)
app.add_exception_handler(pages.NoKnowledgeBase, pages.no_knowledge_base)


def _backup_health(llm) -> dict:
    backup = getattr(llm, "backup", None)
    if backup is None or not backup.available:
        return {"enabled": False}
    return {
        "enabled": True,
        # "" while the startup check runs, then "ok" or the error that stopped it.
        "checked": backup.checked,
        "used": backup.used,
        "heard": backup.heard,
        "models": backup.health(),
        # Everything the key can see: what Groq offers changes, and a claim about it should be read,
        # not remembered (e.g. whether it has an embedding model of its own).
        "on_key": backup.on_key,
    }


def _memory_backup_health(local) -> dict:
    if local is None:
        return {"enabled": False}
    return {"enabled": True, "ready": local.ready, "failed": local.failed, "dimensions": local.dimensions}


@app.get("/health")
async def health() -> dict:
    def enabled(name: str) -> bool:
        return getattr(app.state, name, None) is not None

    activities = getattr(app.state, "activities", {})
    return {
        "status": "ok",
        "whatsapp": enabled("whatsapp"),
        "telegram": enabled("telegram"),
        "database": enabled("store"),
        "indexing": "memory" in activities,
        "answers": enabled("answerer"),
        # The Gemini keys in rotation and each model's health: failures in a row, rest left, latency.
        "gemini_keys": getattr(getattr(app.state, "llm", None), "key_count", 0),
        "models": getattr(app.state, "llm", None).model_health() if getattr(app.state, "llm", None) else [],
        # The spare engine: on or off, and how many answers it has rescued since the start.
        "backup": _backup_health(getattr(app.state, "llm", None)),
        # The memory's spare sense: the local embedding model (Groq has none of its own).
        "memory_backup": _memory_backup_health(getattr(app.state, "local_embedder", None)),
        "daily_digest": "daily_summary" in activities and activities["daily_summary"].enabled,
        "team_report": "team_report" in activities and activities["team_report"].enabled,
    }
