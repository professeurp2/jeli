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


# Greetings / introductions.
_GREETING = re.compile(
    r"(?:"
    r"\bhello\b|\bhi\b|\bhey\b|\bbonjour\b|\bsalut\b|\bsalam\b|\bwelcome\b|bienvenue"
    r"|good\s+(?:morning|afternoon|evening)|bon(?:ne)?\s+(?:matin[eé]e?|soir[eé]e?|journée?|après-midi)"
    r"|how\s+are\s+you|comment\s+(?:ça\s+va|allez-vous|tu\s+vas)"
    r")",
    re.IGNORECASE,
)

# Thanks / gratitude.
_THANKS = re.compile(
    r"(?:"
    r"\bthank(?:s|\s+you)\b|\bmerci\b|\bthx\b|\bthankyou\b"
    r"|je\s+te\s+remercie|je\s+vous\s+remercie|très\s+reconnaissant"
    r")",
    re.IGNORECASE,
)

# Congratulations / celebration / success.
_CONGRATS = re.compile(
    r"(?:"
    r"🎉|🥳|🎊|congrat|félicit|bravo|bien\s+joué|well\s+done|amazing|excellent|fantastique|superbe"
    r"|we\s+(?:won|passed|made\s+it)|on\s+(?:a\s+gagné|a\s+réussi)|c.est\s+(?:génial|parfait|super)"
    r")",
    re.IGNORECASE,
)

# Encouragement / motivation.
_ENCOURAGE = re.compile(
    r"(?:"
    r"💪|🙌|courage|allez|go\s+(?:team|for\s+it)|you\s+can\s+do|on\s+peut\s+le\s+faire|let.s\s+go"
    r")",
    re.IGNORECASE,
)


def emotion_emoji(text: str) -> str | None:
    """Return the emoji to react with based on the message's emotional tone, or None."""
    if _SAD.search(text):
        return "😢"
    if _FUNNY.search(text):
        return "😄"
    if _CONGRATS.search(text):
        return "🎉"
    if _THANKS.search(text):
        return "🙏"
    if _ENCOURAGE.search(text):
        return "💪"
    if _GREETING.search(text):
        return "👋"
    return None


def is_correction(text: str) -> bool:
    """True when a message directed at Jeli appears to correct or criticise its last answer."""
    return bool(_CORRECTION.search(text))
