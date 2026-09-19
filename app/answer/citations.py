"""How sources are shown to members."""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.ingest.transcribe import format_offset, is_youtube
from app.models import Recording

PHONE = re.compile(r"^\+?[\d\s().-]{7,}$")


def is_phone_number(author: str) -> bool:
    return bool(PHONE.match(author.strip()))


def display_author(author: str) -> str:
    """Exports name members by their phone number when the exporting phone didn't know them:
    never show it in full. "+234 818 554 6555" -> "+234 ···55"."""
    if not is_phone_number(author):
        return author
    digits = re.sub(r"\D", "", author)
    prefix = author.strip().split()[0] if author.strip().startswith("+") and " " in author.strip() else ""
    return f"{prefix} ···{digits[-2:]}".strip()


def author_key(author: str) -> str:
    """Compare authors across formats: digits for phone numbers, case-insensitive names otherwise."""
    return re.sub(r"\D", "", author) if is_phone_number(author) else author.strip().lower()


def ignored_keys(authors) -> set[str]:
    return {author_key(author) for author in authors}


def is_ignored(message, keys: set[str]) -> bool:
    """By display name or phone number, and by WhatsApp id: live messages carry a display name,
    not the phone number that exports show."""
    candidates = {author_key(message.author)}
    if message.author_id:
        candidates.add(author_key(message.author_id.split("@")[0]))
    return bool(candidates & keys)


def format_time(moment: datetime) -> str:
    return f"{moment.astimezone(timezone.utc):%d %b %Y, %H:%M} UTC"


def _authors(authors: list[str]) -> str:
    return ", ".join(display_author(a) for a in authors[:2]) + (" …" if len(authors) > 2 else "")


def format_source(number: int, chat_label: str, started_at: datetime, authors: list[str]) -> str:
    return f"[{number}] {chat_label} · {format_time(started_at)} · {_authors(authors)}"


def timestamped_link(url: str | None, offset: timedelta) -> str | None:
    """A YouTube link that starts playing at `offset`; None for other sources."""
    if not url or not is_youtube(url):
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "t"] + [("t", f"{int(offset.total_seconds())}")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def format_recording_source(number: int, recording: Recording, offset: timedelta, authors: list[str]) -> str:
    source = (
        f"[{number}] 🎥 {recording.title} · {recording.recorded_at.astimezone(timezone.utc):%d %b %Y} · "
        f"at {format_offset(offset)} · {_authors(authors)}"
    )
    link = timestamped_link(recording.source_url, offset)
    return f"{source}\n    {link}" if link else source
