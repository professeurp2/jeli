"""Parser for WhatsApp's "Export chat" files (Android and iPhone, any phone language).

A line that starts with a date and a time starts a new message:
    Android   12/09/2026, 14:05 - Awa Traoré: Hello
              12/09/2026 14:05 - Awa Traoré: Bonjour
              9/12/26, 2:05 PM - Awa Traoré: Hello
    iPhone    [12/09/2026, 14:05:33] Awa Traoré: Hello
Any other line continues the previous message. A dated line without "author: " is a system
notice (encryption banner, someone joined…) and is skipped, like media placeholders and
deleted messages, which carry no knowledge. iPhone exports attribute system notices to the
group itself and start their text with an invisible left-to-right mark: those are skipped too.
"""

import hashlib
import io
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Invisible direction marks that iPhone exports sprinkle around names and placeholders.
INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮﻿"))
LEFT_TO_RIGHT_MARK = "‎"

HEADER = re.compile(
    r"^[‎‏﻿]*\[?(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+(?:à\s+)?"
    r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)"
    r"(?:[\s  ]*(?P<ampm>[aApP]\.?\s?[mM]\.?))?"
    r"\]?\s*(?:-\s+)?(?P<rest>.*)$"
)
AUTHOR = re.compile(r"^(?P<author>[^:]{1,80}?):\s(?P<text>.*)$", re.DOTALL)

# Whole messages that are placeholders, in the languages of the cohort.
PLACEHOLDER = re.compile(
    r"^(?:<[^>]*>"  # <Media omitted>, <Médias omis>, <attached: …>
    r"|(?:image|video|vidéo|audio|sticker|gif|document|contact card|photo)\s+(?:omitted|omis|omise|absente?)"
    r"|this message was deleted|you deleted this message"
    r"|ce message a été supprimé|vous avez supprimé ce message"
    r"|null)$",
    re.IGNORECASE,
)
EDITED_MARKER = re.compile(r"\s*<(?:this message was edited|ce message a été modifié)>\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ExportedMessage:
    sent_at: datetime  # timezone-aware
    author: str
    text: str


def read_export(path: Path) -> str:
    """Text of an export: the .txt itself, or the chat file inside WhatsApp's .zip."""
    return read_export_bytes(path.name, path.read_bytes())


def read_export_bytes(filename: str, data: bytes) -> str:
    """The same, from an uploaded file."""
    if filename.lower().endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as error:
            raise ValueError("This .zip file cannot be opened") from error
        with archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            if not names:
                raise ValueError(f"No .txt chat file in {filename}")
            # "_chat.txt" on iPhone, "WhatsApp Chat with …" on Android.
            name = next((n for n in names if n.endswith("_chat.txt")), names[0])
            return archive.read(name).decode("utf-8-sig", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _date_order(dates: list[tuple[int, int, int]], day_first_default: bool) -> str:
    """'ymd', 'dmy' or 'mdy', decided from the whole file: 13/09 can only be day-first."""
    if any(a > 31 for a, _, _ in dates):
        return "ymd"
    if any(a > 12 for a, _, _ in dates):
        return "dmy"
    if any(b > 12 for _, b, _ in dates):
        return "mdy"
    return "dmy" if day_first_default else "mdy"


def _to_datetime(date: tuple[int, int, int], time: str, ampm: str | None, order: str, tz: ZoneInfo) -> datetime:
    a, b, c = date
    year, month, day = {"ymd": (a, b, c), "dmy": (c, b, a), "mdy": (c, a, b)}[order]
    if year < 100:
        year += 2000
    parts = [int(p) for p in re.split(r"[:.]", time)]
    hour, minute, second = parts[0], parts[1], parts[2] if len(parts) > 2 else 0
    if ampm:
        pm = ampm.lower().startswith("p")
        hour = hour % 12 + (12 if pm else 0)
    return datetime(year, month, day, hour, minute, second, tzinfo=tz)


def _clean(text: str) -> str:
    return EDITED_MARKER.sub("", text.translate(INVISIBLE)).strip()


def parse_export(text: str, timezone: str = "UTC", day_first: bool = True) -> list[ExportedMessage]:
    tz = ZoneInfo(timezone)
    raw: list[dict] = []  # header fields + text lines, before dates can be interpreted
    for line in text.splitlines():
        header = HEADER.match(line)
        if header:
            raw.append({**header.groupdict(), "lines": [header["rest"]]})
        elif raw:
            raw[-1]["lines"].append(line)

    dates = [tuple(int(p) for p in re.split(r"[./-]", r["date"])) for r in raw]
    order = _date_order(dates, day_first)

    messages = []
    for fields, date in zip(raw, dates):
        body = AUTHOR.match("\n".join(fields["lines"]))
        if not body or body["text"].startswith(LEFT_TO_RIGHT_MARK):
            continue  # system notice (or an iPhone media placeholder)
        text = _clean(body["text"])
        if not text or PLACEHOLDER.match(text):
            continue
        messages.append(
            ExportedMessage(
                sent_at=_to_datetime(date, fields["time"], fields["ampm"], order, tz),
                author=body["author"].translate(INVISIBLE).strip(),
                text=text,
            )
        )
    return messages


def message_ids(chat_id: str, messages: list[ExportedMessage]) -> list[str]:
    """Stable ids, so importing a newer export of the same chat skips what is already stored.

    Identical messages (same minute, author and text) are told apart by their order of appearance.
    """
    seen: Counter[tuple] = Counter()
    ids = []
    for message in messages:
        key = (message.sent_at.isoformat(), message.author, message.text)
        occurrence = seen[key]
        seen[key] += 1
        digest = hashlib.sha256("\x1f".join((chat_id, *key, str(occurrence))).encode()).hexdigest()
        ids.append(f"export:{digest[:32]}")
    return ids
