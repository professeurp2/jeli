"""What a message is asking for: a catch-up (R8), or a question someone may have answered already (R7)."""

import re
from datetime import datetime, timedelta, timezone

QUESTION_START = re.compile(
    r"^(what|when|where|who|whom|which|why|how|is|are|was|were|can|could|do|does|did|should|will|would|has|have|any"
    r"|quand|où|ou est|comment|quel|quelle|quels|quelles|qui|pourquoi|combien|est-ce|y a-t-il|peut-on|faut-il|a-t-on)\b",
    re.IGNORECASE,
)
URL = re.compile(r"https?://\S+")

# /recap is a session recap (R9), not a catch-up.
CATCHUP_COMMAND = re.compile(r"^/(catchup|catch-up|rattrapage)\b", re.IGNORECASE)
CATCHUP_PHRASE = re.compile(
    r"what (did|have) i miss(ed)?|catch me up|fill me in|what('s| has| is) new|quoi de neuf"
    r"|qu.est.ce que j.ai (raté|manqué|loupé)|j.ai (raté|manqué|loupé) quoi|ce que j.ai (raté|manqué|loupé)",
    re.IGNORECASE,
)
RECAP_WORD = re.compile(r"\b(recap|summary|summari[sz]e|digest|résumé|résume|récap|synthèse)\b", re.IGNORECASE)

WEEKDAYS = {
    "monday": 0, "lundi": 0, "tuesday": 1, "mardi": 1, "wednesday": 2, "mercredi": 2, "thursday": 3, "jeudi": 3,
    "friday": 4, "vendredi": 4, "saturday": 5, "samedi": 5, "sunday": 6, "dimanche": 6,
}
PERIOD = re.compile(
    r"(\d+)\s*(d|days?|jours?|h|hours?|heures?)\b|\b(since|depuis)\s+(" + "|".join(WEEKDAYS) + r")\b"
    r"|\b(today|aujourd.hui|yesterday|hier|this week|cette semaine|last week|la semaine derni[eè]re)\b",
    re.IGNORECASE,
)
DEFAULT_PERIOD = timedelta(hours=24)


RECAP_COMMAND = re.compile(r"^/(recap|résumé|resume)\b", re.IGNORECASE)
SESSION_WORD = re.compile(
    r"\b(session|call|meeting|class|coaching|module|webinar|workshop|réunion|séance|appel|cours|atelier|visio)s?\b",
    re.IGNORECASE,
)
SAID_IN = re.compile(
    r"what (was|were|did they|did we) (said|say|discuss|discussed|cover|covered|decided)|what happened (in|at|during)"
    r"|de quoi (a-t-on|on a|ont-ils) parlé|qu.est-ce qui s.est dit|ce qui s.est dit|qu.a-t-on (dit|décidé)",
    re.IGNORECASE,
)


def is_recap_request(text: str) -> bool:
    """"/recap 2", "summary of the Module 1 session", "de quoi a-t-on parlé pendant le coaching ?"
    A catch-up period is checked first: "recap of this week" is a catch-up."""
    if RECAP_COMMAND.match(text):
        return True
    return bool((RECAP_WORD.search(text) or SAID_IN.search(text)) and SESSION_WORD.search(text))


def looks_like_question(text: str) -> bool:
    """A real question worth checking against the group's history: not a link, not a one-word reply."""
    words = URL.sub("", text).strip()
    return 12 <= len(words) <= 400 and ("?" in words or bool(QUESTION_START.match(words)))


def _midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_since(text: str, now: datetime) -> datetime:
    """Start of the period a catch-up covers: "since Monday", "3 days", "this week", "hier"… (UTC).
    Without a period: the last 24 hours."""
    match = PERIOD.search(text)
    if not match:
        return now - DEFAULT_PERIOD
    amount, unit, _, weekday, named = match.groups()
    if amount:
        delta = timedelta(hours=int(amount)) if unit.lower().startswith("h") else timedelta(days=int(amount))
        return now - delta
    if weekday:
        days_back = (now.weekday() - WEEKDAYS[weekday.lower()]) % 7
        return _midnight(now - timedelta(days=days_back))
    named = named.lower()
    if named in ("today",) or named.startswith("aujourd"):
        return _midnight(now)
    if named in ("yesterday", "hier"):
        return _midnight(now - timedelta(days=1))
    this_monday = _midnight(now - timedelta(days=now.weekday()))
    if named in ("this week", "cette semaine"):
        return this_monday
    return this_monday - timedelta(days=7)  # last week


def catchup_since(text: str, now: datetime | None = None) -> datetime | None:
    """When the message asks for a catch-up, the start of the period to cover; otherwise None.

    "Summarise the Module 1 session" is a question about a session, not a catch-up: a recap word
    only counts with a period ("recap of this week", "résumé depuis lundi").
    """
    now = now or datetime.now(timezone.utc)
    asks = CATCHUP_COMMAND.match(text) or CATCHUP_PHRASE.search(text) or (RECAP_WORD.search(text) and PERIOD.search(text))
    return parse_since(text, now) if asks else None
