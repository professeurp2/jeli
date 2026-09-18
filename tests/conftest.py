import pytest

from app.config import get_settings

CHANNEL_VARS = (
    "WAHA_URL",
    "WAHA_API_KEY",
    "WAHA_WEBHOOK_HMAC_KEY",
    "WHATSAPP_GROUP_IDS",
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
