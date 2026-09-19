"""When scheduled activities run: every day at a time, or every week on a day at a time (UTC)."""

import re
from datetime import datetime, time, timedelta

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def parse_clock(value: str) -> time:
    """"7:05" or "07:05" -> 07:05. ValueError when it is not a time of day."""
    match = re.fullmatch(r"\s*(\d{1,2})[:h.](\d{2})\s*", value)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ValueError("is not a time like 18:00")
    return time(int(match[1]), int(match[2]))


def parse_schedule(value: str) -> tuple[int, time]:
    """"mon 07:00" -> (0, 07:00). ValueError when it is not a day and a time."""
    day, _, clock = value.strip().lower().partition(" ")
    if day[:3] not in WEEKDAYS:
        raise ValueError("is not a day of the week and a time, like mon 07:00")
    return WEEKDAYS.index(day[:3]), parse_clock(clock)


def next_daily(now: datetime, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def next_weekly(now: datetime, weekday: int, at: time) -> datetime:
    candidate = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - now.weekday()) % 7)
    return candidate if candidate > now else candidate + timedelta(days=7)
