"""Settings the team changes from the dashboard, while Jeli runs.

Each one starts from the environment (Railway variables, .env) and, once changed on the dashboard,
is kept in jeli.settings, which wins from then on. Secrets and infrastructure (keys, database, the
team's phone numbers) are never among them. Every change is written to the activity log with who
made it, then applied to the running components (see apply.py).
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.control.schedule import WEEKDAYS, parse_clock, parse_schedule

log = logging.getLogger(__name__)


def _schedule(value: str) -> str:
    day, at = parse_schedule(value)
    return f"{WEEKDAYS[day]} {at:%H:%M}"


def _clock(value: str) -> str:
    return f"{parse_clock(value):%H:%M}"


@dataclass(frozen=True)
class Field:
    key: str
    kind: str  # bool, int, float, text, list, map, time, schedule, choice
    default: Callable[[Settings], Any]
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()


FIELDS = {
    field.key: field
    for field in (
        # Jeli stops answering and posting anywhere; it keeps remembering the conversation.
        Field("paused", "bool", lambda s: False),
        # Members Jeli never answers (names or numbers), and authors never used as sources (other bots).
        Field("muted_members", "list", lambda s: []),
        Field("ignored_authors", "list", lambda s: s.ignored_author_list),
        # Groups Jeli works in; empty: every group its number is in.
        Field("groups", "list", lambda s: sorted(s.whatsapp_groups)),
        # Groups where Jeli reads and remembers everything, but never replies.
        Field("silent_groups", "list", lambda s: []),
        Field("chat_labels", "map", lambda s: s.chat_label_map),
        Field("bot_name", "text", lambda s: s.bot_name),
        # Follow the thread: after an answer, the member's next messages are for Jeli for a while.
        Field("follow_up", "bool", lambda s: True),
        Field("follow_up_minutes", "int", lambda s: 5, 1, 15),
        # A session's recording an organiser shares in a group is added on its own.
        Field("auto_sessions", "bool", lambda s: True),
        # Organisers, beyond the groups' admins (names or numbers).
        Field("organisers", "list", lambda s: s.organiser_list),
        Field("answer_min_similarity", "float", lambda s: s.answer_min_similarity, 0.5, 0.8),
        Field("duplicate_detection", "bool", lambda s: s.duplicate_detection),
        Field("duplicate_min_similarity", "float", lambda s: s.duplicate_min_similarity, 0.6, 0.9),
        Field("duplicate_replies_per_hour", "int", lambda s: s.duplicate_replies_per_hour, 1, 20),
        Field("whatsapp_user_limit", "int", lambda s: s.whatsapp_user_limit, 1, 20),
        Field("whatsapp_hourly_limit", "int", lambda s: s.whatsapp_hourly_limit, 5, 300),
        Field("whatsapp_min_send_interval_seconds", "float", lambda s: s.whatsapp_min_send_interval_seconds, 1, 60),
        Field("daily_digest_time", "time", lambda s: s.daily_digest_time or "18:00"),
        Field("daily_digest_language", "choice", lambda s: s.daily_digest_language, choices=("en", "fr")),
        Field("team_report_time", "schedule", lambda s: s.team_report_time or "mon 07:00"),
        # Background activities the team can switch off. The two that post messages start off
        # unless their schedule was set in the environment.
        Field("enabled.memory", "bool", lambda s: True),
        Field("enabled.deadlines", "bool", lambda s: True),
        Field("enabled.daily_summary", "bool", lambda s: bool(s.daily_digest_time)),
        Field("enabled.team_report", "bool", lambda s: bool(s.team_report_time)),
    )
}


def coerce(field: Field, raw: Any) -> Any:
    """The value in its type, or ValueError with a message for the team."""
    try:
        if field.kind == "bool":
            value = raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "on", "yes")
        elif field.kind in ("int", "float"):
            value = (int if field.kind == "int" else float)(str(raw).strip())
            if (field.minimum is not None and value < field.minimum) or (field.maximum is not None and value > field.maximum):
                raise ValueError(f"must be between {field.minimum:g} and {field.maximum:g}")
        elif field.kind == "text":
            value = " ".join(str(raw).split())[:60]
            if not value:
                raise ValueError("cannot be empty")
        elif field.kind == "list":
            items = raw if isinstance(raw, list) else re.split(r"[,\n]", str(raw))
            value = list(dict.fromkeys(" ".join(str(item).split())[:80] for item in items if str(item).strip()))
        elif field.kind == "map":
            value = {str(k).strip(): " ".join(str(v).split())[:80] for k, v in dict(raw).items() if str(k).strip() and str(v).strip()}
        elif field.kind == "time":
            value = _clock(str(raw))
        elif field.kind == "schedule":
            value = _schedule(str(raw))
        elif field.kind == "choice":
            value = str(raw).strip().lower()
            if value not in field.choices:
                raise ValueError(f"must be one of {', '.join(field.choices)}")
        else:
            raise ValueError(f"unknown kind {field.kind}")
    except (TypeError, ValueError) as error:
        raise ValueError(str(error) or "is not valid") from error
    return value


class Runtime:
    def __init__(self, settings: Settings, store=None):
        self.settings = settings
        self.store = store
        self.overrides: dict[str, Any] = {}
        # Called with the changed keys after each update: components and activities follow.
        self.listeners: list[Callable[[set[str]], None]] = []

    async def load(self) -> None:
        if not self.store:
            return
        for key, value in (await self.store.load_settings()).items():
            if key not in FIELDS:
                continue
            try:
                self.overrides[key] = coerce(FIELDS[key], value)
            except ValueError:
                log.warning("Ignoring the saved value of %s: %r", key, value)

    def __getitem__(self, key: str) -> Any:
        if key in self.overrides:
            return self.overrides[key]
        return FIELDS[key].default(self.settings)

    @property
    def paused(self) -> bool:
        return self["paused"]

    def changed_by_team(self, key: str) -> bool:
        return key in self.overrides

    async def update(self, changes: dict[str, Any], actor: str, note: str) -> set[str]:
        """Validate, save, log and apply. ValueError names the first invalid field; nothing is
        saved then. Returns the keys whose value changed."""
        values = {}
        for key, raw in changes.items():
            try:
                values[key] = coerce(FIELDS[key], raw)
            except ValueError as error:
                raise ValueError(f"{key}: {error}") from error
        changed = {key for key, value in values.items() if value != self[key]}
        if not changed:
            return changed
        if self.store:
            await self.store.save_settings({key: values[key] for key in changed}, actor)
            await self.store.add_audit(actor, note)
        self.overrides.update({key: values[key] for key in changed})
        for listener in self.listeners:
            try:
                listener(changed)
            except Exception:
                log.exception("Could not apply %s", ", ".join(sorted(changed)))
        return changed
