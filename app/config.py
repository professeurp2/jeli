from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables or a local .env file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # WhatsApp through WAHA (self-hosted gateway) — the target channel: Jeli lives in the group.
    waha_url: str = ""  # e.g. http://localhost:3000
    waha_api_key: str = ""
    waha_session: str = "default"
    # Same value as WHATSAPP_HOOK_HMAC_KEY on the WAHA side: every webhook call is signed with it.
    waha_webhook_hmac_key: str = ""
    # Comma-separated group ids (…@g.us) Jeli may listen to. Empty: every group the number is in.
    whatsapp_group_ids: str = ""
    # A group message starting with this name is addressed to the bot, like a mention.
    bot_name: str = "Jeli"
    # Anti-ban limits: WhatsApp restricts accounts that behave like machines.
    whatsapp_user_limit: int = 5  # answers per member…
    whatsapp_user_window_seconds: int = 600  # …within this window
    whatsapp_hourly_limit: int = 120  # answers per hour, all chats together: a runaway loop stops here
    whatsapp_min_send_interval_seconds: float = 3.0  # minimum gap between two messages sent

    # Telegram — fallback channel.
    telegram_bot_token: str = ""
    # Webhook mode when set (deployed), polling mode when empty (local development).
    public_url: str = ""
    telegram_webhook_secret: str = ""

    gemini_api_key: str = ""
    database_url: str = ""
    log_level: str = "INFO"

    @property
    def whatsapp_groups(self) -> set[str]:
        return {group.strip() for group in self.whatsapp_group_ids.split(",") if group.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
