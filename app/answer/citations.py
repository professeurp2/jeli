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


def short_day(moment: datetime) -> str:
    """"Thu 17 Sep": the day, as members read it; no time zone to decode."""
    return f"{moment.astimezone(timezone.utc):%a %d %b}"


def snippet(text: str, limit: int = 180) -> str:
    """One line of a message, cut at a word."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit].rsplit(" ", 1)[0] + " …"


def quote(header: str, *lines: str) -> str:
    """WhatsApp's own way to cite text that cannot be replied to: a quote block ("> " lines)."""
    return "\n".join(f"> {line}" for line in (header, *lines) if line)


def mention_tag(member_id: str) -> str:
    """"@2348012345678": what a WhatsApp message carries to mention a member (shown as @Name)."""
    return "@" + member_id.split("@", 1)[0].split(":", 1)[0]


def timestamped_link(url: str | None, offset: timedelta) -> str | None:
    """A YouTube link that starts playing at `offset`; None for other sources."""
    if not url or not is_youtube(url):
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "t"] + [("t", f"{int(offset.total_seconds())}")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def recording_quote(recording: Recording, offset: timedelta, speaker: str, text: str) -> str:
    """A moment of a call: its title, day and time within the call, what was said, and the link
    that starts playing there."""
    header = f"🎥 *{recording.title}* · {short_day(recording.recorded_at)}, at {format_offset(offset)}"
    return quote(header, f"{speaker}: {snippet(text)}" if speaker else snippet(text), timestamped_link(recording.source_url, offset) or "")
