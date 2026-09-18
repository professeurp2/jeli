from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # WhatsApp Cloud API (Meta, official) — the target channel: members ask questions in DM.
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v25.0"

    # Telegram — default channel while WhatsApp is being set up.
    telegram_bot_token: str = ""
    # Webhook mode when set (deployed), polling mode when empty (local development).
    public_url: str = ""
    telegram_webhook_secret: str = ""

    gemini_api_key: str = ""
    database_url: str = ""
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
