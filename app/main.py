import contextlib
import dataclasses
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import telegram, whatsapp_waha
from app.answer.catchup import Catchup
from app.answer.deadlines import DeadlineExtractor, Deadlines
from app.answer.llm import LLM
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.answer.responder import Responder
from app.answer.understand import Understander
from app.config import get_settings
from app.control.apply import apply
from app.control.guard import Guard
from app.control.runtime import Runtime
from app.control.setup import build_activities
from app.ingest.live import LiveIngestor
from app.kb.embeddings import Embedder
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
    state.embedder = Embedder(settings.gemini_api_key) if settings.gemini_api_key else None

    # The team's settings from the dashboard, over the environment's.
    runtime = Runtime(settings, store)
    await runtime.load()
    state.runtime = runtime
    state.auth = Auth(settings.dashboard_accounts, settings.dashboard_secret, store)

    # Grounded answers need the knowledge base and a Gemini key; without them Jeli says it isn't ready.
    state.llm = state.answerer = state.catchup = state.recaps = state.deadlines = state.extractor = None
    if store and state.embedder:
        state.llm = LLM(settings.gemini_api_key, settings.answer_models)
        state.answerer = Answerer(store, state.embedder, state.llm, min_similarity=runtime["answer_min_similarity"])
        state.deadlines = Deadlines(store)
        state.catchup = Catchup(store, state.llm, deadlines=state.deadlines)
        state.recaps = Recaps(store, state.llm)
        state.extractor = DeadlineExtractor(store, state.llm)
    state.responder = Responder(
        state.answerer,
        state.catchup,
        recaps=state.recaps,
        deadlines=state.deadlines,
        record=store.record_event if store else None,
        understander=Understander(state.llm),
    )
    state.guard = Guard(record=store.record_incident if store else None)

    async def respond(message: IncomingMessage) -> str | None:
        """What the channels call: nothing at all while the team has paused Jeli. A group message
        continuing a conversation with Jeli is for Jeli, without repeating its name."""
        if runtime.paused:
            return None
        if not message.addressed_to_bot and state.responder.is_follow_up(message):
            message = dataclasses.replace(message, addressed_to_bot=True)
        return await state.responder.respond(message)

    # Each adapter runs when its environment variables are set; WhatsApp is the target channel.
    state.whatsapp = whatsapp_waha.start(settings, respond, ingest=LiveIngestor(store).ingest if store else None)
    if state.whatsapp:
        state.whatsapp.guard = state.guard
        state.whatsapp.follow_up = state.responder.is_follow_up
        await state.whatsapp.sync_status()
    state.telegram = await telegram.start(settings, respond)

    # Background activities, then the team's settings applied to everything, now and after each change.
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
        "daily_digest": "daily_summary" in activities and activities["daily_summary"].enabled,
        "team_report": "team_report" in activities and activities["team_report"].enabled,
    }
