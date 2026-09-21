"""Image generation via Pollinations.ai (free, no API key required).

Used when a member asks Jeli to generate or show an image to illustrate a concept.
The LLM first turns the request into an optimised English prompt, then Pollinations
renders it with the FLUX model and returns a JPEG.
"""

import logging
import re
import urllib.parse

import httpx
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

GENERATE_TIMEOUT = 45.0  # Pollinations can be slow on first requests

# Image nouns, articles, and clitic pronouns as named fragments for readability.
_IMG = r"(?:image|photo|illustration|schéma|schema|dessin|diagramm?e?|visuel|figure|picture|diagram|visual|graphic|chart|infographic)"
_ART = r"(?:une?\s+|an?\s+|des\s+|the\s+)?"
# Clitic pronouns that may appear between a verb and an image noun:
#   - _HCLIT: attached via hyphen (fais-moi, montre-lui)
#   - _SCLIT: space-separated (crée moi, envoie lui)
_HCLIT = r"(?:-(?:moi|lui|leur|nous|me|us|them))?"
_SCLIT = r"(?:\s+(?:moi|lui|leur|nous|me|us|them))*"
# Any standard or curly apostrophe (not a raw string so \u escapes are interpreted)
_APO = "['''‘’‛]"

IMAGE_REQUEST = re.compile(
    # Core: trigger verb + optional clitics (hyphen or space) + space + optional article + image noun
    rf"\b(?:génère?|générer|genere?|generer"
    rf"|crée?|créer|cree?|creer"
    rf"|dessine?|dessiner"
    rf"|montre?|montrer"
    rf"|illustre?|illustrer"
    rf"|fais|faire"
    rf"|envoie?|envoyer"
    rf"|partage?|partager"
    rf"|make|draw|show|create|generate|produce|send|share|visualize?)"
    rf"\b{_HCLIT}{_SCLIT}\s+{_ART}{_IMG}\b"
    # "image de/of X"
    rf"|\b{_IMG}\s+(?:de|d{_APO}|du|des|of|about|showing|depicting)\b"
    # "par/avec/with (une) image"
    rf"|\b(?:par|avec|with)\s+{_ART}{_IMG}\b"
    # "répond par/en image"
    rf"|\brepond[sz]?\s+{_HCLIT}{_SCLIT}\s+(?:en|par)\s+{_ART}{_IMG}\b"
    rf"|\brepond[sz]?\s+(?:en|par)\s+{_ART}{_IMG}\b"
    # "sous forme d'image" / "sous forme d'une image" / "sous forme visuelle"
    rf"|\bsous\s+forme\s+(?:d{_APO}(?:une?\s+)?)?(?:{_IMG}|visuelle?)\b"
    # "in image/visual form"
    rf"|\bin\s+(?:image|visual|diagram|picture)\s+form\b",
    re.IGNORECASE,
)

PROMPT_SYSTEM = """\
The user wants an image. Write a concise, vivid image prompt in English for a text-to-image AI.
Focus on what to show visually: subject, composition, style.
For educational or scientific topics prefer styles like "clear educational diagram",
"scientific illustration", "labeled infographic", "step-by-step visual".
Keep the prompt under 80 words. Output only the prompt — no explanation.
Also write a short caption (≤10 words) in the user's language summarising what the image shows.
"""


class ImagePrompt(BaseModel):
    prompt: str
    caption: str


class IllustrationDecision(BaseModel):
    should_illustrate: bool
    prompt: str   # English FLUX prompt; empty string when should_illustrate is False
    caption: str  # Short caption in the question's language; empty when should_illustrate is False


SUGGEST_SYSTEM = """\
You decide whether a WhatsApp answer would be significantly clearer with a visual illustration.

Suggest an image ONLY when the content is genuinely hard to grasp from text alone:
- Several statistics, numbers or proportions → bar chart / pie chart infographic
- Multiple deadlines or a sequence of events → timeline diagram
- Two or more programmes, options or approaches compared side by side → comparison diagram
- A multi-step process or workflow → step-by-step flowchart
- A concept that is inherently spatial, structural or visual → diagram or illustration

Do NOT suggest an image for:
- A single-fact or one-number answer
- A yes / no or very short answer
- A list of names, links or people
- Casual conversation, greetings or thanks
- An answer that is already a direct quote

If yes: write a concise English FLUX prompt (under 60 words). Prefer styles like
"clear educational infographic", "timeline diagram", "comparison chart illustration",
"step-by-step visual guide". Also write a short caption (≤8 words) in the language
of the original question.
"""

# Minimum answer length before considering a proactive image (very short replies never need one).
_MIN_ANSWER_LEN = 120


def asks_for_image(text: str) -> bool:
    return bool(IMAGE_REQUEST.search(text or ""))


async def build_prompt(text: str, llm: LLM) -> ImagePrompt | None:
    """Ask the LLM to turn the user's request into an optimised FLUX image prompt."""
    try:
        return await llm.generate(text, ImagePrompt, system=PROMPT_SYSTEM, attempts=1, temperature=0.5)
    except LLMUnavailable:
        log.warning("Could not build image prompt: LLM unavailable")
        return None


async def suggest_if_useful(question: str, answer: str, llm: LLM) -> ImagePrompt | None:
    """Proactively decide whether the answer would benefit from a visual.

    Returns an ImagePrompt when an image would add real value, None otherwise.
    Runs in background after the text reply is sent — never blocks the user.
    """
    if len(answer) < _MIN_ANSWER_LEN:
        return None
    user_input = f"Question: {question}\n\nAnswer: {answer}"
    try:
        decision = await llm.generate(
            user_input, IllustrationDecision, system=SUGGEST_SYSTEM, attempts=1, temperature=0.2
        )
        if decision.should_illustrate and decision.prompt.strip():
            log.info("Proactive image suggested for question: %.60s", question)
            return ImagePrompt(prompt=decision.prompt, caption=decision.caption)
    except LLMUnavailable:
        log.debug("Proactive image skipped: LLM unavailable")
    return None


async def generate(prompt: str) -> bytes | None:
    """Fetch a FLUX image from Pollinations.ai. Returns JPEG bytes or None on failure."""
    url = (
        "https://image.pollinations.ai/prompt/"
        + urllib.parse.quote(prompt)
        + "?model=flux&width=1024&height=1024&nologo=true"
    )
    try:
        async with httpx.AsyncClient(timeout=GENERATE_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            ct = response.headers.get("content-type", "")
            if ct.startswith("image/"):
                log.info("Image generated (%d bytes) for prompt: %.80s", len(response.content), prompt)
                return response.content
            log.warning("Pollinations returned unexpected content-type: %s", ct)
    except Exception as error:
        log.error("Image generation failed: %s: %s", type(error).__name__, error)
    return None
