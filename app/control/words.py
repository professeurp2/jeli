"""The words the team reads for Jeli's usage, on the dashboard and in the weekly report."""

# Question outcomes, in the fixed order of the dashboard's chart (series 1, 2, 3).
OUTCOMES = [("answered", "Answered"), ("dont_know", "Couldn't answer"), ("sources_only", "Sources only (Jeli was busy)")]
OUTCOME_WORDS = {
    "answered": "answered",
    "dont_know": "couldn't answer",
    "sources_only": "sources only",
    "not_ready": "not ready",
}
# Other ways members use Jeli.
KINDS = [
    ("catchup", "Catch-ups"),
    ("recap", "Session recaps"),
    ("deadlines", "Deadline lists"),
    ("search", "Searches"),
    ("already_answered", "Pointed to an earlier answer"),
]


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"
