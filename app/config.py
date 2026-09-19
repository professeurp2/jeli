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
    # Answer models, tried in order: the next one takes over on quota, overload or timeout.
    gemini_models: str = "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest"
    # Transcription models, tried in order (they listen to the recording, window by window).
    transcription_models: str = "gemini-3.6-flash,gemini-3.5-flash-lite"
    # Below this similarity between the question and the best excerpt, Jeli says it doesn't know
    # without asking the model (measured: group questions ≥ 0.65, unrelated ones ≤ 0.56).
    answer_min_similarity: float = 0.60
    # Authors whose messages are never used in answers, e.g. other bots in the group (comma-separated).
    ignored_authors: str = ""
    # Readable names for chats in citations: "chat-id=Name;other-id=Other name".
    chat_labels: str = ""
    # Supabase Postgres, through the pooler: postgresql://jeli_app.<ref>:<password>@<pooler-host>:5432/postgres
    database_url: str = ""
    # WhatsApp exports carry local times without a timezone: the exporting phone's zone.
    export_timezone: str = "UTC"
    # How often live messages are chunked and embedded.
    index_interval_seconds: int = 300
    log_level: str = "INFO"

    @property
    def whatsapp_groups(self) -> set[str]:
        return {group.strip() for group in self.whatsapp_group_ids.split(",") if group.strip()}

    @property
    def answer_models(self) -> list[str]:
        return [model.strip() for model in self.gemini_models.split(",") if model.strip()]

    @property
    def transcription_model_list(self) -> list[str]:
        return [model.strip() for model in self.transcription_models.split(",") if model.strip()]

    @property
    def ignored_author_list(self) -> list[str]:
        return [author.strip() for author in self.ignored_authors.split(",") if author.strip()]

    @property
    def chat_label_map(self) -> dict[str, str]:
        pairs = (item.split("=", 1) for item in self.chat_labels.split(";") if "=" in item)
        return {chat.strip(): label.strip() for chat, label in pairs}


@lru_cache
def get_settings() -> Settings:
    return Settings()
