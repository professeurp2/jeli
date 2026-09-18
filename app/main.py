import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import telegram, whatsapp_waha
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

    # Each adapter runs when its environment variables are set; WhatsApp is the target channel.
    app.state.whatsapp = whatsapp_waha.start(settings, ingest=LiveIngestor(store).ingest if store else None)
    if app.state.whatsapp:
        await app.state.whatsapp.sync_status()
    app.state.telegram = await telegram.start(settings)
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
    }
