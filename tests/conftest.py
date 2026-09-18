import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Override any local .env so the test suite never starts the real bot."""
    for var in ("TELEGRAM_BOT_TOKEN", "PUBLIC_URL", "TELEGRAM_WEBHOOK_SECRET"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
