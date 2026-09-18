import pytest

from app.config import Settings, get_settings

CHANNEL_VARS = (
    "WAHA_URL",
    "WAHA_API_KEY",
    "WAHA_WEBHOOK_HMAC_KEY",
    "WHATSAPP_GROUP_IDS",
    "TELEGRAM_BOT_TOKEN",
    "PUBLIC_URL",
    "TELEGRAM_WEBHOOK_SECRET",
    "DATABASE_URL",
    "GEMINI_API_KEY",
)


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    """Ignore the local .env and channel variables, so tests never reach a real chat platform."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for var in CHANNEL_VARS:
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
