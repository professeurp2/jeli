"""Emotions: what a member's message feels like — its words, its emoji, or the sticker they sent —
and the reaction a warm, tactful friend would put on it.

A model reads the message (a sticker as an image) instead of word lists: "RIP" and "😂" in the same
message, irony, a 🥹 sticker or a "merci infiniment 🙏🏾" are understood as a person would. It says
the emotion, how strong it is, and the one emoji to react with — or none, for a plain question or
information. The channel decides where that reaction is worth it (see whatsapp_waha.py).
"""

import logging

from google.genai import types
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

EMOTIONS = ("joy", "humor", "sadness", "gratitude", "love", "pride", "encouragement", "surprise", "worry", "frustration", "neutral")
# The emojis Jeli reacts with: warm and unambiguous in every culture of the cohort.
REACTIONS = ("❤️", "😂", "😢", "🙏", "🎉", "👍", "💪", "😮", "🔥", "🥰", "👏", "🤗", "😅", "🙌", "💯")
FEEL_TIMEOUT = 6

FEEL_SYSTEM = f"""\
You feel the emotion of one message posted in the WhatsApp community of African innovators where
Jeli, the community's assistant, lives. The message may be words, emoji alone, or a sticker (then
you see its image). Read it as a person would: irony, several emoji together, a sticker's face.
- emotion: one of {", ".join(EMOTIONS)}.
- strength: 0 no emotion (a plain question, an information, an announcement of a date), 1 light,
  2 clear, 3 strong (grief, a loud laugh, a big success, deep thanks).
- reaction: the single emoji a warm, tactful friend would react with, from: {" ".join(REACTIONS)};
  "" when a reaction would be odd or intrusive (strength 0, a plain question, anything neutral).
- When the words and the emoji disagree, the words decide: a death, an illness, an accident or a
  loss is sadness even with 😂 or 😅 next to it (a slip, or nervousness), and is never laughed at.
  Grief gets 😢, 🙏 or 🤗; worry or frustration gets 🤗 or 🙏.
- is_bad_news: true when the message tells of a death, illness, accident, loss or failure, whatever
  its emoji.
"""
# Laughing reactions: never on bad news, whatever the model felt.
LAUGHS = ("😂", "😅")


class Feeling(BaseModel):
    emotion: str
    strength: int
    reaction: str
    is_bad_news: bool = False


class Emotions:
    def __init__(self, llm: LLM):
        self.llm = llm

    async def feel(self, text: str = "", image: bytes | None = None, mimetype: str = "image/webp") -> Feeling | None:
        """How the message feels and the reaction it calls for; None when no model could tell."""
        contents: list = []
        if image:
            contents.append(types.Part(inline_data=types.Blob(mime_type=mimetype.split(";")[0].strip() or "image/webp", data=image)))
            contents.append("A sticker." + (f" With it: {text[:500]}" if text else ""))
        elif text.strip():
            contents.append(f"Message: {text[:1500]}")
        else:
            return None
        try:
            feeling = await self.llm.generate(contents, Feeling, system=FEEL_SYSTEM, timeout=FEEL_TIMEOUT, temperature=0.2, attempts=2)
        except LLMUnavailable:
            # Measured 23 Sep at 19:35: a member sent a sticker, no model could look at it, and
            # Jeli stayed silent without a word in the logs — the feature looked broken when it
            # was only blind.
            log.info("No model could read this message's feeling: no reaction, no sticker")
            return None
        except Exception:
            # A reaction is a courtesy: a network drop or an unusable answer must never cost the reply.
            log.exception("Could not feel a message's emotion")
            return None
        emotion = feeling.emotion.strip().lower()
        emotion = emotion if emotion in EMOTIONS else "neutral"
        reaction = _known_reaction(feeling.reaction)
        if feeling.is_bad_news:
            # Bad news is never laughed at, even when the model felt its 😂: comfort instead.
            emotion = "sadness" if emotion in ("humor", "joy", "neutral") else emotion
            reaction = "🙏" if reaction in LAUGHS or reaction in ("🎉", "🔥", "🥰", "💯", "🙌", "👏") else reaction
        return Feeling(emotion=emotion, strength=max(0, min(3, feeling.strength)), reaction=reaction, is_bad_news=feeling.is_bad_news)


def _bare(emoji: str) -> str:
    """An emoji without its variation selector or skin tone: "❤" and "❤️", "🙏🏾" and "🙏" match."""
    return "".join(c for c in emoji.strip() if c != "️" and not "\U0001f3fb" <= c <= "\U0001f3ff")


def _known_reaction(emoji: str) -> str:
    return next((known for known in REACTIONS if _bare(known) == _bare(emoji)), "") if emoji.strip() else ""
