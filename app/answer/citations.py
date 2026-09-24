"""How sources are shown to members."""

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.ingest.transcribe import drive_id, format_offset, is_youtube
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


PERSON = re.compile(r"^(?P<name>.*?)[\s,(]*(?P<number>\+?\d[\d\s().-]{6,}\d)\)?\s*$")


def person(entry: str) -> tuple[str, str]:
    """"Diane +250 783 188 655" → ("Diane", "250783188655"); a name or a number alone works too."""
    entry = entry.strip()
    match = PERSON.match(entry)
    if match and match["name"].strip():
        return match["name"].strip(" ,(-"), re.sub(r"\D", "", match["number"])
    if is_phone_number(entry):
        return "", re.sub(r"\D", "", entry)
    return entry, ""


def ignored_keys(authors) -> set[str]:
    """Keys to recognise people by: their name, their number, or both ("Diane +250 783 188 655")."""
    keys = set()
    for entry in authors:
        name, number = person(entry)
        keys |= {k for k in (name.lower(), number) if k}
    return keys


def people_names(entries) -> dict[str, str]:
    """Number (digits) → name, from entries that give both."""
    return {number: name for name, number in map(person, entries) if name and number}


def display_person(entry: str) -> str:
    """"Diane +250 783 188 655" → "Diane (+250 ···55)": the number masked as elsewhere."""
    name, number = person(entry)
    match = PERSON.match(entry.strip())
    masked = display_author(match["number"] if match else entry) if number else ""
    return f"{name} ({masked})" if name and masked else name or masked


# WhatsApp no longer puts the phone number in group messages: each author arrives under a per-
# account id ("LID") that contains nothing of their number. So a person named by number — a bot the
# team muted, an organiser, a teammate — was never recognised there. The channel fills this in as
# it learns the pairs from WhatsApp (app/adapters/whatsapp_waha.py); it stays empty elsewhere, and
# everything then works on names alone, as before.
NUMBER_OF_LID: dict[str, str] = {}


def private_chat_of(author_id: str) -> str:
    """The one-to-one chat with whoever wrote under this id, or "" when WhatsApp has not said.

    A group message carries a LID, and a LID is not a chat anyone can open — the number behind it
    is. So asking Jeli in a group for something to arrive in private only works once WhatsApp has
    told us which number that member is (learn_numbers). When it has not, the caller decides what
    to do about it; guessing a chat id would write to a stranger.
    """
    who = re.sub(r"\D", "", (author_id or "").split("@")[0].split(":")[0])
    if not who:
        return ""
    if author_id.endswith("@c.us"):
        return f"{who}@c.us"
    number = NUMBER_OF_LID.get(who)
    return f"{number}@c.us" if number else ""


def is_ignored(message, keys: set[str]) -> bool:
    """By display name or phone number, and by WhatsApp id: live messages carry a display name,
    not the phone number that exports show."""
    candidates = {author_key(message.author)}
    if message.author_id:
        who = author_key(message.author_id.split("@")[0])
        candidates.add(who)
        number = NUMBER_OF_LID.get(who)
        if number:
            candidates.add(number)
    return bool(candidates & keys)


def short_day(moment: datetime) -> str:
    """"Thu 17 Sep": the day, as members read it; no time zone to decode."""
    return f"{moment.astimezone(timezone.utc):%a %d %b}"


def snippet(text: str, limit: int = 180) -> str:
    """One line of a message, cut at a word."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[:limit].rsplit(" ", 1)[0] + " …"


SENTENCES = re.compile(r"(?<=[.!?])\s+|\n+|\s+•\s*")


def best_snippet(text: str, words: set[str], limit: int = 180) -> str:
    """The sentence of a long text that says most of what the answer says (a document's page, a
    long announcement), rather than its first line."""
    sentences = [s.strip() for s in SENTENCES.split(text) if len(s.strip()) > 20] or [text]
    best = max(sentences, key=lambda s: len(words & {w for w in re.findall(r"\w+", s.lower()) if len(w) > 3}))
    return snippet(best, limit)


def quote(header: str, *lines: str) -> str:
    """WhatsApp's own way to cite text that cannot be replied to: a quote block ("> " lines)."""
    return "\n".join(f"> {line}" for line in (header, *lines) if line)


def mention_tag(member_id: str) -> str:
    """"@2348012345678": what a WhatsApp message carries to mention a member (shown as @Name)."""
    return "@" + member_id.split("@", 1)[0].split(":", 1)[0]


def mention_jid(member_id: str) -> str:
    """JID for the mentions array: device suffix stripped so WhatsApp links @Name correctly.
    '22360557761:12@s.whatsapp.net' → '22360557761@s.whatsapp.net'"""
    user = member_id.split("@", 1)[0].split(":", 1)[0]
    return f"{user}@s.whatsapp.net"


def timestamped_link(url: str | None, offset: timedelta) -> str | None:
    """A YouTube link that starts playing at `offset`; a Drive recording's link as it is (the time is
    in the quote's header); None for other sources."""
    if url and drive_id(url):
        return url
    if not url or not is_youtube(url):
        return None
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "t"] + [("t", f"{int(offset.total_seconds())}")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def recording_quote(recording: Recording, offset: timedelta, speaker: str, text: str, link: bool = True) -> str:
    """A moment of a call: its title, day and time within the call, what was said, and the link
    that starts playing there (`link=False`: not repeated under each moment of the same video)."""
    header = f"🎥 *{recording.title}* · {short_day(recording.recorded_at)}, at {format_offset(offset)}"
    url = timestamped_link(recording.source_url, offset) if link else None
    return quote(header, f"{speaker}: {snippet(text)}" if speaker else snippet(text), url or "")


POLL_MARK = "📊 Poll:"


def poll_text(question: str, options: list[str]) -> str:
    return f"{POLL_MARK} {question}\nOptions: " + " · ".join(options)


# A mention as WhatsApp stores it: "@216324735279308". On screen the app shows the person's name;
# in the text Jeli reads, only the id is there.
RAW_MENTION = re.compile(r"(?<!\w)@(\d{5,})\b")


def named_mentions(text: str, names: dict[str, str]) -> str:
    """Mentions written as people, not as numbers.

    Seen 23 Sep in a catch-up: "*Presentation slides* @216324735279308 asked if the slides…". That
    id is what WhatsApp puts in the message; the app replaces it with a name before anyone sees it,
    and Jeli must do the same before the model reads it — or the model faithfully repeats a number
    that means nothing and looks like a phone number. Someone Jeli cannot name is simply "someone",
    which is true and readable.
    """

    def person(match: re.Match) -> str:
        who = names.get(match.group(1))
        return f"@{who}" if who else "someone"

    return RAW_MENTION.sub(person, text)


def with_tally(text: str, tally: dict[str, int] | None) -> str:
    """A poll's message, with its votes so far."""
    if not text.startswith(POLL_MARK):
        return text
    if not tally:
        return text + "\nNo votes yet."
    ranked = sorted(tally.items(), key=lambda item: -item[1])
    return text + "\nVotes so far: " + " · ".join(f"{option} {n}" for option, n in ranked)
