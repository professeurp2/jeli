"""How sources are shown to members."""

import re
from datetime import datetime, timezone

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


def format_time(moment: datetime) -> str:
    return f"{moment.astimezone(timezone.utc):%d %b %Y, %H:%M} UTC"


def format_source(number: int, chat_label: str, started_at: datetime, authors: list[str]) -> str:
    shown = ", ".join(display_author(a) for a in authors[:2]) + (" …" if len(authors) > 2 else "")
    return f"[{number}] {chat_label} · {format_time(started_at)} · {shown}"
