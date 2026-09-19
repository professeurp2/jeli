"""Emoji reactions: Jeli expresses emotion or acknowledgement without sending a text reply.

Reactions fire on group messages only — never in private chats, never in silent-mode groups.
Pattern matching keeps this instant and free (no LLM call).
"""

import re

# Sad/heavy news: condolences, deaths, tragedies.
_SAD = re.compile(
    r"(?:"
    r"😢|😭|💔"
    r"|rip\b|r\.i\.p"
    r"|rest in peace|repose en paix|repo au paradis"
    r"|condolé|condolen|mes condol"
    r"|décédé|passé de vie|est mort|est décédée|vient de mourir|est décédé"
    r"|passed away|has died|just died|we lost"
    r"|décès|tragedy|tragédie|triste nouvelle|sad news|terrible news"
    r"|priez pour|prayers for|let us pray for"
    r")",
    re.IGNORECASE,
)

# Funny / humorous messages.
_FUNNY = re.compile(
    r"(?:"
    r"😂|🤣|😆|😁|☠️|💀"
    r"|haha+|hihi+|héhé|hehe+"
    r"|mdr+|ptdr|xptdr"
    r"|\blol\b|\blmao\b|\bromfl\b"
    r"|ahahah|ahahaha"
    r")",
    re.IGNORECASE,
)

# A message directed at Jeli that corrects or criticises its answer.
# Use . freely for apostrophe-like characters (covers ', ', U+0027, U+2019, …).
_CORRECTION = re.compile(
    r"(?:"
    r"t.es trompé|vous vous êtes trompé"
    r"|c.est faux|c.est pas correct|c.est pas juste|c.est incorrect"
    r"|mauvaise réponse|tu as tort|vous avez tort"
    r"|wrong answer|incorrect answer|that.s wrong|that.s not right|not correct"
    r"|erreur jeli|jeli.{0,20}(?:wrong|faux|tort|incorrect)"
    r"|you.{0,5}re wrong|you made a mistake|that was wrong"
    r")",
    re.IGNORECASE,
)


def emotion_emoji(text: str) -> str | None:
    """Return the emoji to react with based on the message's emotional tone, or None."""
    if _SAD.search(text):
        return "😢"
    if _FUNNY.search(text):
        return "😄"
    return None


def is_correction(text: str) -> bool:
    """True when a message directed at Jeli appears to correct or criticise its last answer."""
    return bool(_CORRECTION.search(text))
