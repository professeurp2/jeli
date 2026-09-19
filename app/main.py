import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import telegram, whatsapp_waha
from app.answer.catchup import Catchup
from app.answer.llm import LLM
from app.answer.rag import Answerer
from app.answer.responder import Responder
from app.config import get_settings
from app.ingest.live import LiveIngestor
from app.jobs.indexing import index_periodically
from app.kb.embeddings import Embedder
from app.kb.store import Store
from app.system_certificates import use_system_certificates


@asynccontextmanager
async def lifespan(app: FastAPI):
    use_system_certificates()
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO level, and Telegram API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Knowledge base: messages are stored as they arrive, and indexed in the background.
    store = Store(settings.database_url) if settings.database_url else None
    if store:
        await store.open()
    embedder = Embedder(settings.gemini_api_key) if settings.gemini_api_key else None
    indexing = (
        asyncio.create_task(index_periodically(store, embedder, settings.index_interval_seconds))
        if store and embedder
        else None
    )
    app.state.store, app.state.embedder, app.state.indexing = store, embedder, indexing

    # Grounded answers need the knowledge base and a Gemini key; without them Jeli says it isn't ready.
    answerer = catchup = None
    if store and embedder:
        llm = LLM(settings.gemini_api_key, settings.answer_models)
        answerer = Answerer(
            store,
            embedder,
            llm,
            min_similarity=settings.answer_min_similarity,
            ignored_authors=settings.ignored_author_list,
            chat_labels=settings.chat_label_map,
        )
        catchup = Catchup(store, llm, ignored_authors=settings.ignored_author_list, chat_labels=settings.chat_label_map)
    app.state.answerer = answerer
    respond = Responder(
        answerer,
        catchup,
        duplicate_detection=settings.duplicate_detection,
        duplicate_min_similarity=settings.duplicate_min_similarity,
        duplicate_replies_per_hour=settings.duplicate_replies_per_hour,
    ).respond

    # Each adapter runs when its environment variables are set; WhatsApp is the target channel.
    app.state.whatsapp = whatsapp_waha.start(settings, respond, ingest=LiveIngestor(store).ingest if store else None)
    if app.state.whatsapp:
        await app.state.whatsapp.sync_status()
    app.state.telegram = await telegram.start(settings, respond)
    yield
    if app.state.whatsapp:
        await whatsapp_waha.stop(app.state.whatsapp)
    if app.state.telegram:
        await telegram.stop(app.state.telegram)
    if indexing:
        indexing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await indexing
    if store:
        await store.close()


app = FastAPI(title="Jeli", description="Group memory bot", lifespan=lifespan)
app.include_router(whatsapp_waha.router)
app.include_router(telegram.router)


@app.get("/health")
async def health() -> dict:
    def enabled(name: str) -> bool:
        return getattr(app.state, name, None) is not None

    return {
        "status": "ok",
        "whatsapp": enabled("whatsapp"),
        "telegram": enabled("telegram"),
        "database": enabled("store"),
        "indexing": enabled("indexing"),
        "answers": enabled("answerer"),
    }
