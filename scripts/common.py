import asyncio
import logging
import sys
from collections.abc import Coroutine

from app.config import get_settings


def run(main: Coroutine) -> None:
    """asyncio.run, on an event loop psycopg supports (not Windows' default Proactor loop)."""
    # Chat content is full of accents and emoji; Windows consoles default to a legacy code page.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=get_settings().log_level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if sys.platform == "win32":
        asyncio.run(main, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(main)


def require(value: str, name: str) -> str:
    if not value:
        sys.exit(f"{name} is not set (in .env or the environment)")
    return value
