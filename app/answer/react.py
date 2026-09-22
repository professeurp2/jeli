"""Corrections: a member telling Jeli its last answer was wrong ("c'est faux", "that's wrong").

The emotion of messages, emoji and stickers — and the reaction it calls for — is felt by a model,
not by word lists: see app/answer/emotion.py.
"""

import re

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


def is_correction(text: str) -> bool:
    """True when a message directed at Jeli appears to correct or criticise its last answer."""
    return bool(_CORRECTION.search(text))
