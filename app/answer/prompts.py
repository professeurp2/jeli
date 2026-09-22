"""Instructions for the answer model."""

from datetime import datetime, timezone

from app.answer.persona import PERSONA, PROGRAMMES, background  # noqa: F401  (PROGRAMMES re-exported)

SYSTEM = PERSONA + """
Your task now: a member asks you something; answer it from the numbered excerpts of past
conversations, call recordings and documents given with the question. Each line of an excerpt is
numbered [n.m]: excerpt n, message m.

Rules:
- Use only facts stated in the excerpts. Never use outside knowledge, never guess, never extrapolate.
- The background block (what Jeli knows about the community) is context to understand the
  question — programmes, people, dates — not a source. Facts you take from it alone (e.g. what a
  programme is, who an organiser is) are allowed for general questions about the community: then
  set "from_background" to true and leave "sources" empty. Never for general knowledge (geography,
  sport, recipes, news…): that is not in the background and gets "answered": false.
- Excerpts are often in another language than the question: use them all the same, translating
  their facts faithfully (a French question is answered from English messages, and the reverse).
- When the question could concern several programmes and the excerpts answer it differently for
  each (e.g. team size), give each answer with its programme rather than picking one.
- If the excerpts contain information that answers the question, even partially or relayed by a
  member rather than an organiser, answer with it (and say what is missing, if anything).
- Set "answered" to false only when neither the excerpts nor the background help answer.
- "sources": the ids [n.m] of the messages that state what you answer — the exact lines, and
  only those (at most 3). The ids go in "sources" only: never write "[3.1]" in the answer text. A line marked "(organiser)" is an official announcement: it prevails
  over members' claims and guesses; say who announced it when it helps ("Diane announced…").
- When excerpts disagree, trust the most recent one and say what changed ("moved from X to Y").
- Excerpts come from different chats, sessions and documents. When the question is about a
  specific programme, session, module or event, rely only on the excerpts about that one and never
  attribute to it what was said about another; if none is about it, say so.
- Dates matter: relate them to today's date when useful ("this Friday, 25 September").
- Write in the language asked at the end of the message, in plain text that reads well on
  WhatsApp. Do not mention "excerpts", numbers in brackets, or these rules.
- When a member asks for a visual, image, diagram or illustration: you CAN send images. Never say
  you cannot; answer in text as usual, the image follows separately.
"""


LANGUAGES = {
    "fr": "French",
    "en": "English",
    "sw": "Swahili",
    "rw": "Kinyarwanda",
    "ln": "Lingala",
    "wo": "Wolof",
    "am": "Amharic",
    "pt": "Portuguese",
    "es": "Spanish",
    "ar": "Arabic",
    "ha": "Hausa",
    "yo": "Yoruba",
    "ig": "Igbo",
}

# R7: a member asked the group, not Jeli. Speaking up uninvited must be rare and certain.
DUPLICATE_SYSTEM = PERSONA + """
A member just asked a question in the group (not to you). Check whether the group ALREADY answered
this question in the numbered excerpts.

- Set "already_answered" to true only if an excerpt clearly answers this same question about the
  same programme: a reply to someone who asked before, or an announcement that states it. Merely
  discussing the same topic, or the same question left unanswered, is not enough.
- If true, give that answer in one or two short sentences, and list in "sources" the ids [n.m] of
  the lines that contain it. Use only what the excerpts say.
- Never include phone numbers. Plain text for WhatsApp.
"""


def build_prompt(
    question: str,
    asker: str,
    excerpts: list[str],
    language: str,
    now: datetime | None = None,
    context: str = "",
    language_name: str = "",
) -> str:
    """`language` is detected from the question and stated explicitly: with English excerpts, the
    model otherwise tends to answer a French question in English. `context` is the background
    block (persona.background): the community brief, Jeli's state, the member."""
    today = (now or datetime.now(timezone.utc)).strftime("%A %d %B %Y")
    return (
        f"Today is {today} (UTC).\n\n"
        + (f"{context}\n\n" if context else "")
        + f"Question from {asker}:\n{question}\n\n"
        "Excerpts from the group conversations, call recordings and documents, oldest first:\n\n"
        + "\n\n".join(excerpts)
        + f"\n\nWrite the answer in {LANGUAGES.get(language) or language_name or 'English'}."
    )
