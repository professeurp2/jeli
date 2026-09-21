"""Image generation: Imagen 3 (primary, via Gemini API keys) → Pollinations FLUX (fallback).

Used when a member asks Jeli to generate or show an image to illustrate a concept.
The LLM first turns the request into an optimised English prompt, then Imagen 3
renders it and returns a JPEG. Pollinations is used when all Gemini keys fail.
"""

import asyncio
import logging
import re
import urllib.parse

import httpx
from google.genai import types
from pydantic import BaseModel

from app.answer.llm import LLM, LLMUnavailable

log = logging.getLogger(__name__)

IMAGEN_MODEL = "imagen-3.0-generate-001"
IMAGEN_TIMEOUT = 30.0
GENERATE_TIMEOUT = 45.0  # Pollinations fallback — can be slow on first requests

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
The user wants an image. Write a concise image prompt in English for a text-to-image AI.
ALWAYS use infographic / diagram style: flat design, clean white background, bold labels,
minimal clutter, high contrast colours. Never photorealistic, never blurry.
Examples of good style endings: "flat vector infographic, white background, bold text labels",
"clean data visualization, minimal design", "educational diagram, clear layout".
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

If yes: write a concise English FLUX prompt (under 55 words, NOT including the mandatory suffix below).
ALWAYS end the prompt with this exact suffix (do not paraphrase it):
"flat vector infographic, white background, bold text labels, high contrast colours, clean minimal design"
Never dark background, never photorealistic, never blurry.
Also write a short caption (≤8 words) in the language of the original question.
"""

# Minimum answer length before considering a proactive image (very short replies never need one).
_MIN_ANSWER_LEN = 120


def asks_for_image(text: str) -> bool:
    return bool(IMAGE_REQUEST.search(text or ""))


# --- Topic extraction -----------------------------------------------------------

# 1. Remove trailing image qualifiers ("par une image", "repond par image", …)
_STRIP_TAIL = re.compile(
    r"\s*[,.]?\s*(?:par|avec|with)\s+(?:une?\s+|an?\s+)?(?:image|photo|illustration|schéma|schema|dessin|diagramm?e?|visuel|figure|picture|diagram|visual|graphic|chart|infographic)\s*$"
    rf"|\s*[,.]?\s*repond[sz]?\s+(?:en|par)\s+(?:une?\s+)?(?:image|photo|visuel|illustration|schéma)\s*$"
    rf"|\s*sous\s+forme\s+(?:d{_APO}(?:une?\s+)?)?(?:image|visuelle?)\s*$"
    r"|\s*in\s+(?:image|visual|diagram|picture)\s+form\s*$",
    re.IGNORECASE,
)
# 2. Remove leading trigger verb (+ optional clitics)
_STRIP_HEAD = re.compile(
    r"^\s*(?:génère?|générer|genere?|generer|crée?|créer|cree?|creer|dessine?|dessiner"
    r"|montre?|montrer|illustre?|illustrer|fais|faire|envoie?|envoyer|partage?|partager"
    r"|make|draw|show|create|generate|produce|send|share|visualize?)\b"
    r"\s*(?:-?(?:moi|lui|leur|nous|me|us|them)\s*)*",
    re.IGNORECASE,
)
# 3. Remove remaining "une image de / a diagram of" noun phrase
_STRIP_NOUN = re.compile(
    rf"(?:une?\s+|an?\s+)?(?:image|photo|illustration|schéma|schema|dessin|diagramm?e?|visuel|figure|picture|diagram|visual|graphic|chart|infographic)\s+(?:de|d{_APO}|du|des|of|about|sur|showing|depicting|concernant)?\s*",
    re.IGNORECASE,
)


_STATS_PATTERN = re.compile(
    r"\d+\s*%"                              # percentage: 60%, 80 %
    r"|\d+\s*/\s*\d+"                       # fraction: 3/4, 1/3
    r"|\b(?:taux|score|proportion|ratio|moyenne|average|rate"
    r"|pourcentage|percent|statistique|chiffre|données?|data)\b",
    re.IGNORECASE,
)


def has_statistical_content(text: str) -> bool:
    """Return True when the text contains statistics worth illustrating as a chart."""
    return bool(_STATS_PATTERN.search(text or ""))


def topic_from_request(text: str) -> str:
    """Extract the subject to illustrate, stripping trigger verbs and image-noun phrases.
    Returns an empty string when the request has no identifiable subject
    (e.g. bare "génère une image" with nothing else).
    """
    t = _STRIP_TAIL.sub("", text or "").strip()
    t = _STRIP_HEAD.sub("", t).strip()
    t = _STRIP_NOUN.sub("", t).strip(" ,.;:!?")
    return " ".join(t.split())


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
            prompt = decision.prompt.strip().rstrip(".,")
            _STYLE = "flat vector infographic, white background, bold text labels, high contrast colours"
            if "infographic" not in prompt.lower() and "flat" not in prompt.lower():
                prompt = f"{prompt}, {_STYLE}"
            return ImagePrompt(prompt=prompt, caption=decision.caption)
    except LLMUnavailable:
        log.debug("Proactive image skipped: LLM unavailable")
    return None


async def _imagen_generate(prompt: str, llm: LLM) -> bytes | None:
    """Generate with Imagen 3 using all available Gemini API keys. Returns JPEG bytes or None."""
    if not llm._clients:
        return None
    config = types.GenerateImagesConfig(
        number_of_images=1,
        output_mime_type="image/jpeg",
        aspect_ratio="1:1",
    )
    for idx, client in enumerate(llm._clients):
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_images(
                    model=IMAGEN_MODEL,
                    prompt=prompt,
                    config=config,
                ),
                timeout=IMAGEN_TIMEOUT,
            )
            if response.generated_images:
                data = response.generated_images[0].image.image_bytes
                if data:
                    log.info("Imagen 3 generated image (%d bytes) for prompt: %.80s", len(data), prompt)
                    return data
        except Exception as error:
            log.warning("Imagen 3 key %d failed: %s: %s", idx, type(error).__name__, error)
    return None


async def generate(prompt: str, llm: LLM | None = None) -> bytes | None:
    """Generate an image. Tries Imagen 3 first (via Gemini keys), falls back to Pollinations FLUX."""
    if llm is not None:
        data = await _imagen_generate(prompt, llm)
        if data:
            return data
        log.info("Imagen 3 failed for all keys — falling back to Pollinations")
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
                log.info("Pollinations generated image (%d bytes) for prompt: %.80s", len(response.content), prompt)
                return response.content
            log.warning("Pollinations returned unexpected content-type: %s", ct)
    except Exception as error:
        log.error("Image generation failed: %s: %s", type(error).__name__, error)
    return None
