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

IMAGE_REQUEST = re.compile(
    r"\b(?:génère?|genere?|générer|generer|crée?|créer|dessine?|montre?|illustre?|illustrer|fais\s+une?|make|draw|show|create|generate|produce)\s+(?:une?\s+|an?\s+)?(?:image|photo|illustration|schéma|schema|dessin|diagramm?e?|visuel|figure|picture|diagram|visual|graphic|chart|infographic)\b"
    r"|\bimage\s+(?:de|d[''‛]|du|des|of|about|showing|depicting)\b"
    r"|\bmontre[- ]moi\s+(?:une?\s+)?(?:image|photo|illustration|schéma|dessin|diagramme|visuel)\b"
    r"|\bshow\s+me\s+(?:a\s+|an\s+)?(?:image|picture|diagram|visual|chart|illustration)\b",
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


def asks_for_image(text: str) -> bool:
    return bool(IMAGE_REQUEST.search(text or ""))


async def build_prompt(text: str, llm: LLM) -> ImagePrompt | None:
    """Ask the LLM to turn the user's request into an optimised FLUX image prompt."""
    try:
        return await llm.generate(text, ImagePrompt, system=PROMPT_SYSTEM, attempts=1, temperature=0.5)
    except LLMUnavailable:
        log.warning("Could not build image prompt: LLM unavailable")
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
