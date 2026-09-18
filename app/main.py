import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.adapters import telegram
from app.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO level, and Telegram API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    app.state.telegram = await telegram.start(settings)
    yield
    if app.state.telegram:
        await telegram.stop(app.state.telegram)


app = FastAPI(title="Jeli", description="Group memory bot", lifespan=lifespan)
app.include_router(telegram.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "telegram": getattr(app.state, "telegram", None) is not None}
