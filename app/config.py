import re
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
    # A member's share of answers in 24 h (0: no limit). Past it Jeli keeps answering, in writing
    # only: voice notes are by far the biggest cost, and one person cannot spend everyone's day.
    member_daily_limit: int = 40
    whatsapp_min_send_interval_seconds: float = 3.0  # minimum gap between two messages sent

    # Telegram — fallback channel.
    telegram_bot_token: str = ""
    # Webhook mode when set (deployed), polling mode when empty (local development).
    public_url: str = ""
    telegram_webhook_secret: str = ""

    gemini_api_key: str = ""
    # Extra Gemini keys (comma-separated) for quota rotation: when one key is exhausted the next
    # takes over. Do not create projects to multiply the free quota: on 22 Sep Google suspended 8
    # of Jeli's projects for it (Terms of Service). More quota: billing on one project.
    gemini_api_keys: str = ""
    # Answer models, tried in order: the next one takes over on quota, overload or timeout. Each has
    # its own free daily quota per key: gemini-3.1-flash-lite (checked 22 Sep, slower) adds a reserve
    # when the others are spent or overloaded, without another project.
    gemini_models: str = "gemini-3.6-flash,gemini-3-flash-preview,gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-flash-lite-latest"
    # Light models, for everything but the answers members read (understanding, emotions, voice,
    # reminders, deadline finding, documents…). Measured on AI Studio (22 Sep, free tier, per key and
    # day): gemini-3.6-flash allows 20 requests, the Flash-Lite models 500 — used first for every call,
    # the 20 were gone by the morning.
    # Measured 23 Sep at 17:30, on a key, one model at a time: gemini-3.1-flash-lite answered 503
    # "this model is currently experiencing high demand", 3.5-flash-lite and flash-lite-latest did
    # not answer at all within 20 s, and gemini-3.6-flash answered in 2.9 s. Every model of this
    # tier was down, so the rotation was working perfectly — it was rotating between dead models,
    # and every call fell through to the spare engine, which burned 197,831 of its 200,000 free
    # tokens for the day by 17:16. The best model closes the list: 20 a day per key over 16 keys is
    # a real reserve, and it is only reached when the cheap ones are genuinely unavailable.
    light_models: str = "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3-flash-preview,gemini-3.6-flash"
    # Groq, the spare engine: a free key from console.groq.com, used only for text answers when
    # every Gemini model has refused (22 Sep: "high demand" on all of them at once). Empty: off.
    groq_api_key: str = ""
    groq_models: str = "llama-3.3-70b-versatile"
    # Transcription models, tried in order (they listen to the recording, window by window).
    transcription_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-3.6-flash"
    # The memory's spare sense: embeddings computed on our own server, so the group's memory stays
    # searchable when Google is unreachable (Groq has no embedding model). Off: Gemini only.
    local_embeddings: bool = True
    # Below this similarity between the question and the best excerpt, Jeli says it doesn't know
    # without asking the model (measured: group questions ≥ 0.65, unrelated ones ≤ 0.56).
    answer_min_similarity: float = 0.60
    # Authors whose messages are never used in answers, e.g. other bots in the group (comma-separated).
    ignored_authors: str = ""
    # R7: when a member asks the group a question it already answered, Jeli points to that answer,
    # uninvited. Stricter than normal answers, and capped per group to keep a low profile.
    duplicate_detection: bool = True
    duplicate_min_similarity: float = 0.70
    duplicate_replies_per_hour: int = 3
    # R13: accounts of the web dashboard at /dashboard, one per team member: "name:salt:hash,…",
    # made by `python -m scripts.dashboard_users` (passwords are never stored). Empty: no dashboard.
    dashboard_users: str = ""
    # Signs the dashboard's session cookies. Empty: a new one at each start (members sign in again).
    dashboard_secret: str = ""
    # R10: "HH:MM" (UTC) to post a daily digest in each group of WHATSAPP_GROUP_IDS. Empty: off.
    daily_digest_time: str = ""
    daily_digest_language: str = "en"
    # Weekly report to the team, in private: "mon 07:00" (UTC). Empty: off.
    team_report_time: str = ""
    # The team members' WhatsApp numbers, comma-separated. Personal data: set it on the server only.
    # These are the only people allowed to use admin commands (/silence, /resume).
    team_numbers: str = ""
    # The one number that may steer Jeli in plain words from WhatsApp (app/control/admin.py).
    # Personal data, and it decides who commands Jeli: set it on the server only, never in the repo.
    super_admin_number: str = ""
    # Public URL of Jeli's WhatsApp profile picture (set once at startup via WAHA API). Empty: no change.
    bot_picture_url: str = ""
    # The community's organisers (names and/or numbers, comma-separated): their messages are
    # announcements. The groups' admins count too. Server only; the team edits it on the dashboard.
    organisers: str = ""
    # Set by Railway when the service has a public domain: the report links to the dashboard.
    railway_public_domain: str = ""
    # Readable names for chats in citations: "chat-id=Name;other-id=Other name".
    chat_labels: str = ""
    # Supabase Postgres, through the pooler: postgresql://jeli_app.<ref>:<password>@<pooler-host>:5432/postgres
    database_url: str = ""
    # WhatsApp exports carry local times without a timezone: the exporting phone's zone.
    export_timezone: str = "UTC"
    # How often live messages are chunked and embedded. Set to 30 on Railway for faster recall.
    # The other half of the wait before a new message is searchable (see SETTLE in
    # app/control/setup.py). A round with nothing to learn is two cheap queries and no model
    # call, so polling often costs nothing anyone pays for.
    index_interval_seconds: int = 10
    log_level: str = "INFO"

    @property
    def api_key_list(self) -> list[str]:
        """All Gemini keys for quota rotation, deduped. Both env vars accept comma-separated lists."""
        primary = [k.strip() for k in self.gemini_api_key.split(",") if k.strip()]
        extras = [k.strip() for k in self.gemini_api_keys.split(",") if k.strip()]
        seen = set(primary)
        return primary + [k for k in extras if k not in seen]

    @property
    def whatsapp_groups(self) -> set[str]:
        return {group.strip() for group in self.whatsapp_group_ids.split(",") if group.strip()}

    @property
    def light_model_list(self) -> list[str]:
        return [model.strip() for model in self.light_models.split(",") if model.strip()]

    @property
    def groq_model_list(self) -> list[str]:
        return [model.strip() for model in self.groq_models.split(",") if model.strip()]

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
    def dashboard_accounts(self) -> dict[str, str]:
        """Account name (lower case) → "salt:hash"."""
        pairs = (item.strip().split(":", 1) for item in self.dashboard_users.split(",") if ":" in item)
        return {name.strip().lower(): secret.strip() for name, secret in pairs if name.strip()}

    @property
    def organiser_list(self) -> list[str]:
        return [person.strip() for person in self.organisers.split(",") if person.strip()]

    @property
    def team_number_list(self) -> list[str]:
        """Digits only: "+234 706 931 0683" -> "2347069310683"."""
        numbers = (re.sub(r"\D", "", number) for number in self.team_numbers.split(","))
        return list(dict.fromkeys(number for number in numbers if number))

    @property
    def chat_label_map(self) -> dict[str, str]:
        pairs = (item.split("=", 1) for item in self.chat_labels.split(";") if "=" in item)
        return {chat.strip(): label.strip() for chat, label in pairs}


@lru_cache
def get_settings() -> Settings:
    return Settings()
