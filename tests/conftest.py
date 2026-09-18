import pytest

from app.config import get_settings

CHANNEL_VARS = (
    "WHATSAPP_ACCESS_TOKEN",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "PUBLIC_URL",
    "TELEGRAM_WEBHOOK_SECRET",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Override any local .env so the test suite never reaches a real chat platform."""
    for var in CHANNEL_VARS:
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
