"""Jeli's own page, for the members of the community — not for the team.

The dashboard at /dashboard belongs to the five people who run Jeli: it changes how Jeli behaves.
This page belongs to everyone else. It shows what Jeli is, what it can do and how it is doing, and
lets a member try it — and it can change nothing at all.

Signing in is the member's own number. Not a password: the fact that Jeli has already heard them
in the groups. That is enough here, because there is nothing to protect — the page shows what the
community sees every day, and the chat is capped so it cannot become a second, quieter WhatsApp.

Ten messages, two of them spoken. Then Jeli says where the real conversation happens and gives the
link. The team and the super admin are not counted: they are testing, not visiting.
"""

import dataclasses
import logging
import re
import secrets
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.answer.voice import asks_for_voice, spoken, without_voice_request
from app.config import get_settings
from app.control.admin import is_super_admin
from app.models import IncomingMessage
from app.web import ui
from app.web.auth import VISITOR_COOKIE, auth_of, is_https
from app.web.ui import esc, icon

log = logging.getLogger(__name__)

router = APIRouter()

# What a visitor may try before Jeli points them to WhatsApp. Two of the ten may be spoken: a voice
# note costs far more than a written answer, and the point here is to show what Jeli does, not to
# serve a whole conversation.
MESSAGES_ALLOWED = 10
VOICE_ALLOWED = 2
# The number Jeli lives on, for the link at the end (set from the settings; "" hides the link).
WHATSAPP_LINK = "https://wa.me/{number}"

EXAMPLES = [
    "C'est quand la prochaine session ?",
    "Qu'est-ce que j'ai manqué depuis lundi ?",
    "What are the deadlines this week?",
    "Résume-moi la dernière session enregistrée",
]


def _digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def _unlimited(number: str) -> bool:
    """The team and the super admin are testing, not visiting: they are never capped."""
    settings = get_settings()
    return any(number.endswith(team) for team in settings.team_number_list) or is_super_admin(
        settings.super_admin_number, number
    )


class Visits:
    """How much each visitor has used, since the last deployment. In memory on purpose: this is a
    courtesy limit on a demo page, not an account."""

    def __init__(self):
        self.messages: dict[str, int] = {}
        self.voices: dict[str, int] = {}

    def left(self, number: str) -> tuple[int, int]:
        if _unlimited(number):
            return MESSAGES_ALLOWED, VOICE_ALLOWED
        return (
            max(0, MESSAGES_ALLOWED - self.messages.get(number, 0)),
            max(0, VOICE_ALLOWED - self.voices.get(number, 0)),
        )

    def used(self, number: str, spoken_reply: bool) -> None:
        if _unlimited(number):
            return
        self.messages[number] = self.messages.get(number, 0) + 1
        if spoken_reply:
            self.voices[number] = self.voices.get(number, 0) + 1


def _visits(request: Request) -> Visits:
    state = request.app.state
    if not hasattr(state, "visits"):
        state.visits = Visits()
    return state.visits


def _who(request: Request) -> tuple[str, str] | None:
    return auth_of(request).visitor(request.cookies.get(VISITOR_COOKIE))


def _whatsapp_link() -> str:
    number = _digits(get_settings().team_numbers.split(",")[0] if get_settings().team_numbers else "")
    return WHATSAPP_LINK.format(number=number) if number else ""


# --- The page ------------------------------------------------------------------------------------


def _shell(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{esc(title)} · Jeli</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{ui.CSS}</style></head>
<body class="login-body"><main class="public">{body}</main></body></html>"""


@router.get("/jeli", response_class=HTMLResponse)
async def public_page(request: Request) -> HTMLResponse:
    who = _who(request)
    if who is None:
        return HTMLResponse(_shell("Entrer", _sign_in_form()))
    number, name = who
    state = request.app.state
    left, voice_left = _visits(request).left(number)
    counts = await _counts(getattr(state, "store", None))
    body = (
        f'<div class="login-brand"><span class="login-avatar">{ui.LEMUR}</span><h1>Jeli</h1>'
        f"<p>Bonjour {esc(name.split()[0] if name else '')} 👋</p></div>"
        + _what_jeli_knows(counts)
        + _what_jeli_does()
        + _chat(left, voice_left)
        + '<p class="login-foot">Cette page ne change rien : elle montre. Pour de vrai, '
        + 'Jeli vous répond sur WhatsApp, dans le groupe.</p>'
    )
    return HTMLResponse(_shell("Jeli", body))


def _sign_in_form(error: str = "") -> str:
    message = ui.notice("bad", esc(error)) if error else ""
    return (
        f'<div class="login-brand"><span class="login-avatar">{ui.LEMUR}</span><h1>Jeli</h1>'
        "<p>Le griot de la communauté</p></div>"
        + message
        + """<form method="post" action="/jeli/enter" class="login-form">
        <label>Votre numéro WhatsApp<input name="number" inputmode="tel" placeholder="+223 …" required autofocus></label>
        """
        + ui.button("Entrer", kind="primary wide")
        + """</form>
        <p class="login-foot">Le même numéro que dans le groupe. Jeli vous reconnaît parce qu'il vous
        a déjà lu — il n'y a pas de mot de passe, et il n'y a rien à changer ici.</p>"""
    )


async def _counts(store) -> dict:
    """What Jeli holds, read now. Never a number Jeli cannot stand behind: 0 when it cannot say."""
    if store is None:
        return {}
    try:
        overview = await store.knowledge_overview()
    except Exception:
        log.warning("Could not read what Jeli knows for the public page", exc_info=True)
        return {}
    chats = overview.get("chats", [])
    return {
        "messages": sum(chat["messages"] for chat in chats),
        "recordings": len(overview.get("recordings", [])),
        "deadlines": (overview.get("totals") or {}).get("deadlines", 0),
    }


def _what_jeli_knows(counts: dict) -> str:
    """A few honest numbers: what Jeli holds, read from the memory itself."""
    stats = "".join(
        ui.stat(label, value, sub)
        for label, value, sub in (
            ("Messages gardés", f"{counts.get('messages', 0):,}".replace(",", " "), "des groupes suivis"),
            ("Sessions écoutées", str(counts.get("recordings", 0)), "enregistrements transcrits"),
            ("Échéances suivies", str(counts.get("deadlines", 0)), "à venir"),
        )
    )
    return ui.card("Ce que Jeli garde", f'<div class="stats">{stats}</div>', icon_name="book")


def _what_jeli_does() -> str:
    rows = "".join(
        f'<div class="row"><div class="row-text"><b>{esc(title)}</b><span>{esc(text)}</span></div></div>'
        for title, text in (
            ("Il répond avec ses sources", "Chaque réponse dit qui l'a dit, où et quand. Quand il ne sait pas, il le dit."),
            ("Il rattrape ce que vous avez manqué", "« Qu'est-ce que j'ai manqué depuis lundi ? » — le résumé, pas la liste."),
            ("Il écoute les sessions", "Les enregistrements partagés dans le groupe sont transcrits et résumés."),
            ("Il vous rappelle à temps", "« Rappelle-moi avant la réunion » — il vous prévient."),
            ("Il parle votre langue", "Français, anglais, swahili, kinyarwanda… par écrit ou en vocal."),
            ("Il ne parle que si on lui parle", "Jamais de message non sollicité dans le groupe."),
        )
    )
    return ui.card("Ce qu'il sait faire", f'<div class="rows">{rows}</div>', icon_name="sparkle")


def _chat(left: int, voice_left: int) -> str:
    chips = "".join(f'<button type="button" class="chip" data-example="{esc(e)}">{esc(e)}</button>' for e in EXAMPLES)
    link = _whatsapp_link()
    return ui.card(
        "Essayez",
        f"""<div class="wa">
          <div class="wa-chat" id="chat-log" aria-live="polite"></div>
          <form class="wa-compose" id="composer">
            <div class="chips">{chips}</div>
            <div class="wa-row"><textarea name="text" id="text" placeholder="Écrivez à Jeli" required maxlength="1000" rows="1"></textarea>
            <button type="submit" class="wa-send" aria-label="Envoyer">{icon("send", 20)}</button></div>
            <p class="hint" id="left">Il vous reste {left} message{'s' if left != 1 else ''}, dont {voice_left} en vocal.
            Ajoutez « réponds en vocal » pour entendre sa voix.</p>
          </form>
        </div>"""
        + (f'<p class="hint"><a href="{esc(link)}">Continuer sur WhatsApp</a></p>' if link else ""),
        icon_name="chat",
        description="Ce que vous écrivez ici n'est pas envoyé dans le groupe.",
    ) + _script()


def _script() -> str:
    return """<script>
    const log = document.getElementById('chat-log'), form = document.getElementById('composer');
    const text = document.getElementById('text'), left = document.getElementById('left');
    document.querySelectorAll('.chip').forEach(c => c.onclick = () => { text.value = c.dataset.example; text.focus(); });
    function show(role, body, audio) {
      const el = document.createElement('div');
      el.className = 'wa-msg ' + (role === 'me' ? 'me' : 'them');
      el.innerHTML = body.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
      if (audio) { const a = document.createElement('audio'); a.controls = true; a.src = audio; el.appendChild(a); }
      log.appendChild(el); log.scrollTop = log.scrollHeight;
    }
    form.onsubmit = async (e) => {
      e.preventDefault();
      const said = text.value.trim(); if (!said) return;
      show('me', said); text.value = ''; text.disabled = true;
      const r = await fetch('/jeli/ask', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: said }) });
      const data = await r.json();
      show('them', data.reply || data.error || 'Je n\\'ai pas pu répondre.', data.audio);
      if (data.left !== undefined) left.textContent = data.left_text;
      text.disabled = !!data.finished; if (!data.finished) text.focus();
    };
    </script>"""


# --- Entering and leaving -------------------------------------------------------------------------


@router.post("/jeli/enter", response_model=None)
async def enter(request: Request):
    form = await request.form()
    number = _digits(str(form.get("number", "")))
    store = getattr(request.app.state, "store", None)
    if len(number) < 8 or store is None:
        return HTMLResponse(_shell("Entrer", _sign_in_form("Entrez le numéro complet, avec l'indicatif du pays.")))
    # WhatsApp gives groups a per-account id, not the phone number: ask the channel to translate,
    # then look the member up under either (app/adapters/whatsapp_waha.py).
    channel = getattr(request.app.state, "whatsapp", None)
    ids = await channel.ids_for_number(number) if channel is not None else [number]
    name = await store.member_by_ids(ids)
    if not name:
        return HTMLResponse(
            _shell("Entrer", _sign_in_form("Jeli ne reconnaît pas ce numéro : il ne vous a pas encore lu dans le groupe."))
        )
    response = RedirectResponse("/jeli", status_code=303)
    response.set_cookie(
        VISITOR_COOKIE,
        auth_of(request).new_visitor(number, name),
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        max_age=24 * 3600,
    )
    log.info("A member opened Jeli's public page")
    return response


@router.get("/jeli/leave")
async def leave(request: Request) -> RedirectResponse:
    response = RedirectResponse("/jeli", status_code=303)
    response.delete_cookie(VISITOR_COOKIE)
    return response


# --- The capped chat ------------------------------------------------------------------------------


@router.post("/jeli/ask")
async def ask(request: Request) -> JSONResponse:
    who = _who(request)
    if who is None:
        return JSONResponse({"error": "Entrez votre numéro d'abord."}, status_code=401)
    number, name = who
    visits = _visits(request)
    left, voice_left = visits.left(number)
    link = _whatsapp_link()
    if left <= 0:
        return JSONResponse(
            {
                "reply": "On a bien discuté ici 😊 La vraie conversation se passe sur WhatsApp, dans le groupe : "
                + ("écrivez-moi là-bas, je réponds à tout le monde." if not link else f"écrivez-moi là-bas : {link}"),
                "finished": True,
                "left": 0,
                "left_text": "Continuons sur WhatsApp.",
            }
        )
    data = await request.json()
    said = " ".join(str(data.get("text", "")).split())[:1000]
    if not said:
        return JSONResponse({"error": "Écrivez quelque chose."}, status_code=400)
    state = request.app.state
    if getattr(state, "responder", None) is None:
        return JSONResponse({"error": "Jeli n'est pas prêt pour l'instant."}, status_code=503)
    voice = getattr(state, "voice", None)
    by_voice = voice is not None and asks_for_voice(said) and voice_left > 0
    message = IncomingMessage(
        platform="dashboard",
        chat_id=f"public:{number}",
        message_id=secrets.token_hex(8),
        author=name or "Un membre",
        text=without_voice_request(said) if by_voice else said,
        sent_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        is_private=True,
        addressed_to_bot=True,
    )
    started = time.monotonic()
    reply = await state.responder.respond(message)
    audio = None
    if reply and by_voice:
        try:
            sound = await voice.speak(spoken(reply), getattr(reply, "language", ""))
            if sound:
                import base64

                audio = "data:audio/mpeg;base64," + base64.b64encode(sound).decode()
        except Exception:
            log.warning("Could not speak on the public page", exc_info=True)
    visits.used(number, audio is not None)
    left, voice_left = visits.left(number)
    log.info("Public page answered in %.1f s, %d left for this visitor", time.monotonic() - started, left)
    return JSONResponse(
        {
            "reply": str(reply) if reply else "Je n'ai rien trouvé là-dessus.",
            "audio": audio,
            "left": left,
            "finished": left <= 0,
            "left_text": (
                f"Il vous reste {left} message{'s' if left != 1 else ''}, dont {voice_left} en vocal."
                if left
                else "C'était le dernier ici — continuons sur WhatsApp."
            ),
        }
    )
