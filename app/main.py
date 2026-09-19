import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import time

from fastapi import FastAPI

from app import dashboard
from app.adapters import telegram, whatsapp_waha
from app.answer.catchup import Catchup
from app.answer.deadlines import DeadlineExtractor, Deadlines
from app.answer.llm import LLM
from app.answer.rag import Answerer
from app.answer.recaps import Recaps
from app.answer.responder import Responder
from app.config import get_settings
from app.ingest.live import LiveIngestor
from app.jobs.daily_digest import run_daily
from app.jobs.deadlines import extract_periodically
from app.jobs.indexing import index_periodically
from app.jobs.team_report import parse_schedule, run_weekly
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
    answerer = catchup = recaps = deadlines = llm = deadline_scan = None
    background = [task for task in (indexing,) if task]
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
        deadlines = Deadlines(store, settings.chat_label_map)
        catchup = Catchup(
            store, llm, ignored_authors=settings.ignored_author_list, chat_labels=settings.chat_label_map, deadlines=deadlines
        )
        recaps = Recaps(store, llm)
        extractor = DeadlineExtractor(store, llm, settings.ignored_author_list, settings.chat_label_map)
        deadline_scan = asyncio.create_task(extract_periodically(extractor))
        background.append(deadline_scan)
    app.state.answerer, app.state.llm, app.state.deadline_scan = answerer, llm, deadline_scan
    respond = Responder(
        answerer,
        catchup,
        duplicate_detection=settings.duplicate_detection,
        duplicate_min_similarity=settings.duplicate_min_similarity,
        duplicate_replies_per_hour=settings.duplicate_replies_per_hour,
        recaps=recaps,
        deadlines=deadlines,
        record=store.record_event if store else None,
    ).respond

    # Each adapter runs when its environment variables are set; WhatsApp is the target channel.
    app.state.whatsapp = whatsapp_waha.start(settings, respond, ingest=LiveIngestor(store).ingest if store else None)
    if app.state.whatsapp:
        await app.state.whatsapp.sync_status()
    app.state.telegram = await telegram.start(settings, respond)

    # R10: the daily digest, only when a time is set and there are groups to post in.
    app.state.daily_digest = None
    if settings.daily_digest_time and catchup and app.state.whatsapp and settings.whatsapp_groups:
        app.state.daily_digest = asyncio.create_task(
            run_daily(
                store,
                catchup,
                app.state.whatsapp.post,
                sorted(settings.whatsapp_groups),
                time.fromisoformat(settings.daily_digest_time),
                settings.daily_digest_language,
            )
        )
        background.append(app.state.daily_digest)
    elif settings.daily_digest_time:
        logging.getLogger(__name__).warning("DAILY_DIGEST_TIME is set, but it needs WhatsApp, the knowledge base and WHATSAPP_GROUP_IDS")

    # Weekly report to the team, in private, when a schedule and the team's numbers are set.
    app.state.team_report = None
    if settings.team_report_time:
        try:
            weekday, at = parse_schedule(settings.team_report_time)
        except ValueError:
            logging.getLogger(__name__).error("TEAM_REPORT_TIME must look like 'mon 07:00': no team report")
        else:
            if store and app.state.whatsapp and settings.team_number_list:
                dashboard_url = f"https://{settings.railway_public_domain}/dashboard" if settings.railway_public_domain else ""
                app.state.team_report = asyncio.create_task(
                    run_weekly(
                        store, deadlines, app.state.whatsapp.post_private, settings.team_number_list, weekday, at, dashboard_url
                    )
                )
                background.append(app.state.team_report)
            else:
                logging.getLogger(__name__).warning("TEAM_REPORT_TIME is set, but it needs WhatsApp, the knowledge base and TEAM_NUMBERS")
    yield
    if app.state.whatsapp:
        await whatsapp_waha.stop(app.state.whatsapp)
    if app.state.telegram:
        await telegram.stop(app.state.telegram)
    for task in background:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    if store:
        await store.close()


# No public API documentation: the only public pages are /health and the password-protected dashboard.
app = FastAPI(title="Jeli", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(whatsapp_waha.router)
app.include_router(telegram.router)
app.include_router(dashboard.router)


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
        "daily_digest": enabled("daily_digest"),
        "team_report": enabled("team_report"),
    }
