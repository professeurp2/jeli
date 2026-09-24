"""Talking to Jeli out loud, from a browser — no phone number, no app, no account.

WhatsApp does not let a program answer a call, and Teams needs an administrator's blessing. A
browser needs neither: the page takes the microphone, the audio goes to Gemini's Live API, and
Jeli answers in its own voice, interrupting and being interrupted like a person.

It answers from the community's memory, not from general knowledge. Two things carry that: what
Jeli already knows without looking (the brief and its own state, the same block every written
answer starts from), and a tool it can call mid-sentence — `search_memory` — which runs the very
search behind /search and the answers in the groups. So a caller can ask "when do the tests close?"
and hear the answer with who announced it, in the second it takes to say it.

The Live API is the one part of Jeli that cannot run out: measured on AI Studio on 23 September,
its requests per day are unlimited — only tokens per minute are capped.
"""

import asyncio
import contextlib
import json
import logging
import secrets
import time

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from google import genai
from google.genai import types

from app.answer.persona import CAPABILITIES, PERSONA, background
from app.control.runtime import DEFAULT_CALL_QUESTIONS
from app.web import ui
from app.web.ui import esc

log = logging.getLogger(__name__)

router = APIRouter()

# The Live API's own models, newest first. Their requests per day are unlimited.
CALL_MODELS = ["gemini-2.5-flash-native-audio-latest", "gemini-3.8-live", "gemini-3.1-flash-live-preview"]
VOICE = "Aoede"  # when the team has chosen nothing
# A call ends for one of three reasons, and none of them is a stopwatch: the caller hangs up, nobody
# has spoken for a long time, or no Live model will take the call any more. Measured 23 Sep: a
# ten-minute wall cut conversations that were still going, and a session ending — which Google does
# routinely, every few minutes — ended the whole call instead of being reopened.
QUIET_SECONDS = 300
CALLS_CLOSED = "Jeli ne prend pas d'appels pour le moment."
GIVE_UP_AFTER = 4
RECONNECT_PAUSE = 1.0
SHORTEST_REAL_SESSION = 3.0
# What the caller hears if nothing can answer.
NO_ENGINE = "Jeli ne peut pas prendre l'appel pour l'instant."
RESUMING = "Un instant — je reprends…"
GONE_QUIET = "Personne ne parlait depuis un moment, alors j'ai raccroché. Rappelez quand vous voulez."

SPOKEN = """
You are on a voice call now, not writing a message. So: short sentences, one idea at a time, the
way a person answers out loud. No lists, no bullet points, no formatting — say "three things:
first… then… and finally…". Never read a URL aloud; say where it is instead. When you do not know,
say so in one sentence and offer the next step.

Before answering anything about what the community said, decided, or scheduled, call search_memory
and answer from what it returns, naming who said it and when. Never answer such a question from
general knowledge, and never invent a date.
"""

SEARCH_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="search_memory",
            description=(
                "Search the community's memory — the groups' messages, the transcribed sessions and "
                "the shared documents — and return the passages that answer a question, with who "
                "said each one and when. Call this for anything about what was said, decided, "
                "announced or scheduled."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "question": types.Schema(
                        type=types.Type.STRING,
                        description="The caller's question, written out in full, in their language.",
                    )
                },
                required=["question"],
            ),
        )
    ]
)


async def _instructions(state) -> str:
    """Who Jeli is, what it knows without looking, and how to speak on a call."""
    brief = getattr(getattr(state, "brief", None), "text", "") or ""
    awareness = ""
    if getattr(state, "awareness", None) is not None:
        try:
            awareness = await state.awareness.state()
        except Exception:
            log.warning("Could not read Jeli's state for a call", exc_info=True)
    return PERSONA + CAPABILITIES + SPOKEN + "\n\n" + background(brief, awareness, "")


async def _search(state, question: str) -> str:
    """The same search as the groups' answers, as text the voice can read out."""
    answerer = getattr(state, "answerer", None)
    if answerer is None:
        return "The memory is not available right now."
    try:
        reply = await answerer.answer(question, asker="a caller", member="")
    except Exception:
        log.warning("A call's memory search failed", exc_info=True)
        return "The memory could not be searched right now."
    return str(reply)[:4000] or "Nothing in the community's memory answers that."


# --- The page -------------------------------------------------------------------------------------


def call_settings(app_state) -> dict:
    """What the team has decided about calls, with the defaults when there is no runtime (tests)."""
    runtime = getattr(app_state, "runtime", None)

    def chosen(key, fallback):
        try:
            value = runtime[key]
        except Exception:
            return fallback
        return fallback if value is None else value

    # "same": Jeli sounds like itself everywhere — the voice of its voice notes, on the call too.
    voice = str(chosen("call_voice", "same"))
    if voice == "same":
        voice = str(chosen("voice_name", "aoede"))
    return {
        "on": bool(chosen("enabled.calls", True)),
        "voice": voice.title(),
        "quiet": int(chosen("call_quiet_minutes", 5)),
        "transcript": bool(chosen("call_transcript", True)),
        "questions": [q for q in chosen("call_questions", list(DEFAULT_CALL_QUESTIONS)) if str(q).strip()][:6],
    }


async def _counts(app_state) -> list[tuple[str, str]]:
    """What Jeli keeps, in figures. The page says what it is before anyone has to believe it."""
    store = getattr(app_state, "store", None)
    if store is None or not hasattr(store, "knowledge_overview"):
        return []
    try:
        overview = await store.knowledge_overview()
    except Exception:
        log.info("Could not read the figures for the call page", exc_info=True)
        return []
    chats = overview.get("chats") or []
    recordings = overview.get("recordings") or []
    messages = sum(int(c["messages"]) for c in chats)
    groups = sum(1 for c in chats if str(c["chat_id"]).endswith("@g.us"))
    figures = [
        (f"{messages:,}".replace(",", " "), "messages en mémoire"),
        (str(groups), "groupes suivis"),
        (str(len([r for r in recordings if r["method"] != "link"])), "sessions transcrites"),
    ]
    return [(value, label) for value, label in figures if value not in ("0", "")]


@router.get("/jeli/call", response_class=HTMLResponse)
async def call_page(request: Request) -> HTMLResponse:
    chosen = call_settings(request.app.state)
    if not chosen["on"]:
        return HTMLResponse(_shell(CLOSED_BODY))
    figures = "".join(
        f'<div class="fig"><b>{esc(value)}</b><span>{esc(label)}</span></div>' for value, label in await _counts(request.app.state)
    )
    chips = "".join(
        f'<button class="chip" type="button" data-ask="{esc(q)}">{esc(q)}</button>' for q in chosen["questions"]
    )
    body = (
        '<header class="call-top">'
        + f'<span class="call-mark">{ui.LEMUR}</span>'
        + "<h1>Parlez à Jeli</h1>"
        + "<p>Il écoute, cherche dans la mémoire du groupe, et vous répond de vive voix.</p>"
        + "</header>"
        + '<section class="stage" id="stage" data-state="idle">'
        + '<div class="orb-wrap">'
        + '<canvas id="orb" width="560" height="560" aria-hidden="true"></canvas>'
        + JELI_FACE
        + "</div>"
        + '<p class="status" id="status">Appuyez pour appeler</p>'
        + '<button id="talk" class="dial" type="button" aria-label="Appeler Jeli">'
        + f'<span class="dial-icon">{ui.icon("call", 30)}</span></button>'
        + '<p class="stage-note" id="note">Votre micro ne s\'ouvre qu\'une fois l\'appel commencé.</p>'
        + "</section>"
        + (f'<div class="chips" id="chips"><p class="chips-lead">Ou touchez une question — il l\'entendra :</p>{chips}</div>' if chips else "")
        + ('<section class="said" id="said" hidden></section>' if chosen["transcript"] else "")
        + (f'<div class="figs">{figures}</div>' if figures else "")
        # A page that promises privacy and is listened to anyway is a page that lies, and these are
        # the community's own members. So the sentence says both things, plainly.
        + '<p class="call-foot">Rien n\'est enregistré : ce que vous dites sert à répondre, puis disparaît.'
        + "<br>L'équipe de Jeli peut suivre un appel en direct, pour vérifier qu'il répond bien.</p>"
        + _script(chosen)
    )
    return HTMLResponse(_shell(f'<main class="call-page">{body}</main>'))


# Jeli's own face, the one members see on WhatsApp — not a stock avatar. It is the same lemur as
# ui.LEMUR, taken apart so each piece can move: the ears perk up when it is listening, the eyes
# blink and follow what it is doing, and the mouth opens on the loudness of its own voice. That
# last one is the point: a caller watching the face is watching Jeli speak, not a loading spinner.
JELI_FACE = """<svg id="face" viewBox="0 0 64 64" aria-hidden="true">
<g id="head">
<circle cx="32" cy="34" r="26" fill="#2a2c32" stroke="#ffffff" stroke-width="2.4"/>
<g id="earL"><path d="M12 21 17 8l9 11z" fill="#cfcbc1"/></g>
<g id="earR"><path d="M52 21 47 8l-9 11z" fill="#cfcbc1"/></g>
<g id="crown"><path d="M25.5 19.5 25.5 13.5l3.2 2.6L32 9.8l3.3 6.3 3.2-2.6v6z" fill="#f0b429"/>
  <circle cx="32" cy="17.2" r="1.3" fill="#2ea44f"/></g>
<ellipse cx="32" cy="35" rx="19" ry="17" fill="#f1eee7"/>
<path d="M32 18c-3 0-4 4-4 8h8c0-4-1-8-4-8z" fill="#2a2c32"/>
<g id="eyeL"><ellipse cx="23.5" cy="32" rx="7.4" ry="7.8" fill="#2a2c32"/>
  <circle cx="23.5" cy="32" r="4.8" fill="#f5b301"/>
  <circle id="pupilL" cx="24" cy="32.4" r="2.1" fill="#141518"/></g>
<g id="eyeR"><ellipse cx="40.5" cy="32" rx="7.4" ry="7.8" fill="#2a2c32"/>
  <circle cx="40.5" cy="32" r="4.8" fill="#f5b301"/>
  <circle id="pupilR" cx="41" cy="32.4" r="2.1" fill="#141518"/></g>
<path id="smile" d="M29 42.5q3 3.2 6 0z" fill="#2a2c32"/>
<ellipse id="mouth" cx="32" cy="43.4" rx="4.2" ry="0" fill="#2a2c32" opacity="0"/>
</g></svg>"""


CLOSED_BODY = (
    '<main class="call-page"><header class="call-top">'
    + "<h1>La ligne est fermée</h1>"
    + "<p>Jeli ne prend pas d'appels pour le moment. Écrivez-lui sur WhatsApp — il répond, "
    + "et il peut aussi répondre par vocal.</p></header></main>"
)


def _shell(body: str) -> str:
    return (
        """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="theme-color" content="#0b2b25"><title>Appeler Jeli</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>"""
        + ui.CSS
        + CALL_CSS
        + "</style></head><body class=\"call-body\">"
        + body
        + "</body></html>"
    )


# The call has its own screen. Everything else Jeli shows is a document to read; this one is a
# device to use, so it is dark, centred and alive whatever the rest of the site is doing.
CALL_CSS = """
/* The call has its own screen, and it wears Jeli's own colours — the ones on the profile picture
   members already see on WhatsApp: a deep jungle green, a white ring around its face, and the
   tropical accents of the crown and the flowers. Everything else Jeli shows is a document to read;
   this one is a device to use. */
.call-body { --jungle: #123c34; --jungle-deep: #0b2b25; --leaf: #1d5b4e; --ring: #ffffff;
  --gold: #f0b429; --hibiscus: #ff5f9e; --sky: #3fb9e8; --lime: #6fc96b; --sand: #f3efe4;
  --ink: #f3efe4; --ink-soft: #a9c4bb; --ink-faint: #7ea79b;
  margin: 0; min-height: 100vh; color: var(--ink); background:
  radial-gradient(1000px 640px at 50% -18%, #1b5348, var(--jungle) 55%, var(--jungle-deep) 100%),
  var(--jungle-deep); }
.call-page { max-width: 560px; margin: 0 auto; padding: max(24px, env(safe-area-inset-top)) 16px
  calc(40px + env(safe-area-inset-bottom)); display: grid; gap: 22px; }
.call-top { text-align: center; display: grid; justify-items: center; gap: 8px; }
/* The white circle of the profile picture, around the same face. */
.call-mark { width: 74px; height: 74px; border-radius: 50%; background: var(--leaf);
  box-shadow: 0 0 0 3px var(--ring), 0 8px 24px rgba(0,0,0,.35); display: grid; place-items: center;
  overflow: hidden; }
.call-mark svg { width: 74px; height: 74px; }
.call-top h1 { font-size: clamp(23px, 6.4vw, 30px); font-weight: 700; color: #fff;
  letter-spacing: 0.01em; }
.call-top p { margin: 0; color: var(--ink-soft); max-width: 34ch; font-size: 14.5px; }

.stage { position: relative; display: grid; justify-items: center; gap: 14px;
  padding: clamp(18px, 5vw, 28px) 16px 24px; border-radius: 28px;
  background: linear-gradient(180deg, rgba(255,255,255,.07), rgba(255,255,255,.02));
  border: 1px solid rgba(255,255,255,.12); }
.orb-wrap { position: relative; display: grid; place-items: center; }
#orb { width: clamp(180px, 54vw, 260px); height: clamp(180px, 54vw, 260px); display: block; margin: -6px 0 -4px; }
/* Jeli sits inside its own aura: the canvas is the halo, this is the face in the middle of it. */
#face { position: absolute; width: 46%; height: 46%; pointer-events: none; }
.status { margin: 0; min-height: 22px; font-size: 15.5px; font-weight: 600; color: #fff;
  letter-spacing: -0.01em; text-align: center; }
.stage-note { margin: 0; font-size: 12.5px; color: var(--ink-faint); text-align: center; max-width: 32ch; }

.dial { width: 76px; height: 76px; border-radius: 50%; border: none; cursor: pointer;
  display: grid; place-items: center; color: #fff; background: #1f9d55;
  box-shadow: 0 0 0 3px rgba(255,255,255,.9), 0 10px 30px rgba(31,157,85,.45);
  transition: transform .15s ease, background .2s ease, box-shadow .2s ease; }
.dial:hover { transform: translateY(-2px); }
.dial:active { transform: scale(.95); }
.dial:focus-visible { outline: 3px solid var(--gold); outline-offset: 5px; }
.stage[data-state="dialing"] .dial { background: #6b8a82; box-shadow: 0 0 0 3px rgba(255,255,255,.7); }
.stage:not([data-state="idle"]):not([data-state="ended"]) .dial { background: #d6402f;
  box-shadow: 0 0 0 3px rgba(255,255,255,.9), 0 10px 30px rgba(214,64,47,.45); }
.stage:not([data-state="idle"]):not([data-state="ended"]) .dial-icon { transform: rotate(135deg); }
.dial-icon { display: grid; transition: transform .25s ease; }

.chips { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; }
.chips-lead { width: 100%; margin: 0 0 2px; text-align: center; color: var(--ink-soft); font-size: 13px; }
.chip { border: 1px solid rgba(255,255,255,.2); background: rgba(255,255,255,.07); color: #eef5f2;
  border-radius: 999px; padding: 9px 14px; font: inherit; font-size: 13.5px; cursor: pointer;
  transition: background .18s ease, border-color .18s ease, transform .12s ease; }
.chip:hover { background: rgba(255,95,158,.18); border-color: var(--hibiscus); color: #fff; }
.chip:active { transform: scale(.97); }
.chip[disabled] { opacity: .45; cursor: default; }

/* minmax(0, 1fr), not the implicit `auto`: an auto column sizes to its widest bubble, and one
   long sentence then pushed the whole page wider than the phone it was being read on. */
.said { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; max-height: 46vh;
  overflow-y: auto; padding: 4px 2px; scroll-behavior: smooth; }
.bubble { max-width: 86%; padding: 10px 13px; border-radius: 16px; font-size: 14.5px;
  line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere;
  animation: rise .28s ease both; }
.bubble.you { justify-self: end; background: #1f7f6d; color: #fff; border-bottom-right-radius: 5px; }
.bubble.jeli { justify-self: start; background: rgba(255,255,255,.1); color: #eef5f2;
  border-bottom-left-radius: 5px; }
.bubble.doing { justify-self: center; max-width: 100%; text-align: center; background: none;
  color: var(--ink-faint); font-size: 12.5px; padding: 2px 8px; }
.bubble.doing b { color: var(--hibiscus); font-weight: 600; }
@keyframes rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }

.figs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px;
  padding-top: 14px; border-top: 1px solid rgba(255,255,255,.12); }
.fig { text-align: center; }
.fig b { display: block; font-size: 19px; color: var(--gold); font-weight: 700; letter-spacing: -0.02em; }
.fig span { font-size: 11.5px; color: var(--ink-faint); line-height: 1.3; display: block; }
.call-foot { margin: 0; text-align: center; color: var(--ink-faint); font-size: 12px; }

@media (max-width: 380px) {
  .call-page { padding-left: 12px; padding-right: 12px; gap: 18px; }
  .dial { width: 68px; height: 68px; }
  .chip { padding: 8px 12px; font-size: 13px; }
}
@media (prefers-reduced-motion: reduce) {
  .bubble { animation: none; }
  .dial, .dial-icon, .chip { transition: none; }
}
"""

def _script(chosen: dict) -> str:
    """The orb, the line, and the state they share.

    Both levels the orb reacts to are real: the caller's own microphone on the way out, and Jeli's
    voice on the way in, each measured from the samples already passing through. Nothing here is a
    decorative loop pretending to listen.
    """
    return (
        "<script>const SHOW_TRANSCRIPT = "
        + ("true" if chosen["transcript"] else "false")
        + ";"
        + CALL_SCRIPT
        + "</script>"
    )


CALL_SCRIPT = r"""
const stage = document.getElementById('stage'), status = document.getElementById('status');
const note = document.getElementById('note'), talk = document.getElementById('talk');
const said = document.getElementById('said'), canvas = document.getElementById('orb');
const chips = document.getElementById('chips');
let socket, mic, context, out, playAt = 0, speakingUntil = 0, pending = null;

/* --- the orb ------------------------------------------------------------------------------- */
const ctx = canvas.getContext('2d');
const TONE = { idle: '#f0b429', dialing: '#8fb3aa', listening: '#3fb9e8',
               searching: '#ff5f9e', speaking: '#ffc247', ended: '#6f9a90' };
const STILL = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
let level = 0, want = 0, phase = 0;

function fit() {
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const size = Math.max(1, canvas.clientWidth);
  canvas.width = canvas.height = Math.round(size * ratio);
}
function shade(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + alpha + ')';
}
function draw() {
  const w = canvas.width, mid = w / 2, unit = w / 2;
  const tone = TONE[stage.dataset.state] || TONE.idle;
  const base = unit * 0.46;
  ctx.clearRect(0, 0, w, w);
  const halo = ctx.createRadialGradient(mid, mid, base * 0.3, mid, mid, unit);
  halo.addColorStop(0, shade(tone, 0.30 + level * 0.25));
  halo.addColorStop(1, shade(tone, 0));
  ctx.fillStyle = halo;
  ctx.fillRect(0, 0, w, w);
  for (let ring = 0; ring < 3; ring++) {
    const wobble = Math.sin(phase * (1 + ring * 0.33) + ring * 2.1) * 0.04;
    const radius = base * (1.06 + ring * 0.22 + wobble) + level * unit * 0.26 * (1 - ring * 0.25);
    ctx.beginPath();
    ctx.arc(mid, mid, radius, 0, Math.PI * 2);
    ctx.strokeStyle = shade(tone, 0.45 - ring * 0.13);
    ctx.lineWidth = Math.max(1, unit * 0.011);
    ctx.stroke();
  }
  /* A rim, not a disc: the middle belongs to Jeli's face, and a filled core would bury it. */
  ctx.beginPath();
  ctx.arc(mid, mid, base * (0.86 + level * 0.06), 0, Math.PI * 2);
  ctx.strokeStyle = shade(tone, 0.85);
  ctx.lineWidth = Math.max(2, unit * (0.018 + level * 0.02));
  ctx.shadowColor = shade(tone, 0.9);
  ctx.shadowBlur = unit * (0.09 + level * 0.14);
  ctx.stroke();
  ctx.shadowBlur = 0;
  if (stage.dataset.state === 'searching') {
    for (let dot = 0; dot < 3; dot++) {
      const angle = phase * 1.6 + dot * (Math.PI * 2 / 3);
      const away = base * 1.62;
      ctx.beginPath();
      ctx.arc(mid + Math.cos(angle) * away, mid + Math.sin(angle) * away, unit * 0.026, 0, Math.PI * 2);
      ctx.fillStyle = shade(tone, 0.9);
      ctx.fill();
    }
  }
}
/* --- Jeli's face -----------------------------------------------------------------------------
   The same lemur members see on WhatsApp, given something to do. What it does is not decoration:
   the mouth opens on the measured loudness of Jeli's own voice, so watching the face is watching
   Jeli speak. The rest is the manners that make it read as someone rather than something —
   it blinks, it looks where it is working, its ears go up when it is listening to you. */
const head = document.getElementById('head'), mouth = document.getElementById('mouth');
const smile = document.getElementById('smile'), earL = document.getElementById('earL');
const earR = document.getElementById('earR'), eyeL = document.getElementById('eyeL');
const eyeR = document.getElementById('eyeR'), pupilL = document.getElementById('pupilL');
const pupilR = document.getElementById('pupilR');
/* Per state: how far the ears lift, where the eyes rest, how wide they open. */
const MOOD = {
  idle:      { ears: 0,  look: [0, 0],     open: 1,    sway: 0.25 },
  dialing:   { ears: 4,  look: [0, -0.5],  open: 1,    sway: 0.2 },
  listening: { ears: 13, look: [0, 0.2],   open: 1.1,  sway: 0.35 },
  searching: { ears: -6, look: [1.5, -1.6], open: 0.9, sway: 0.8 },
  speaking:  { ears: 5,  look: [0, 0],     open: 1,    sway: 0.6 },
  ended:     { ears: -9, look: [0, 0.6],   open: 0.45, sway: 0.1 },
};
let ears = 0, lookX = 0, lookY = 0, opened = 1, blinkUntil = 0, nextBlink = 0;

function ease(now, to, by) { return now + (to - now) * by; }

function face(at) {
  const mood = MOOD[stage.dataset.state] || MOOD.idle;
  ears = ease(ears, mood.ears, 0.08);
  lookX = ease(lookX, mood.look[0], 0.06);
  lookY = ease(lookY, mood.look[1], 0.06);
  /* A blink every few seconds, never twice in the same breath. */
  if (at > nextBlink) { blinkUntil = at + 110; nextBlink = at + 2600 + Math.random() * 3600; }
  const blinking = at < blinkUntil;
  opened = ease(opened, blinking ? 0.07 : mood.open, blinking ? 0.55 : 0.3);
  earL.setAttribute('transform', 'rotate(' + (-ears) + ' 19 20)');
  earR.setAttribute('transform', 'rotate(' + ears + ' 45 20)');
  eyeL.setAttribute('transform', 'translate(23.5 32) scale(1 ' + opened.toFixed(3) + ') translate(-23.5 -32)');
  eyeR.setAttribute('transform', 'translate(40.5 32) scale(1 ' + opened.toFixed(3) + ') translate(-40.5 -32)');
  pupilL.setAttribute('cx', (24 + lookX).toFixed(2));
  pupilR.setAttribute('cx', (41 + lookX).toFixed(2));
  pupilL.setAttribute('cy', (32.4 + lookY).toFixed(2));
  pupilR.setAttribute('cy', (32.4 + lookY).toFixed(2));
  /* The mouth is the voice: closed it is a smile, open it is Jeli talking. */
  const speaking = stage.dataset.state === 'speaking';
  const gap = speaking ? level * 5.2 : 0;
  mouth.setAttribute('ry', gap.toFixed(2));
  mouth.setAttribute('rx', (4.2 - gap * 0.18).toFixed(2));
  mouth.setAttribute('opacity', gap > 0.25 ? 1 : 0);
  smile.setAttribute('opacity', gap > 0.25 ? 0 : 1);
  const tilt = STILL ? 0 : Math.sin(phase * 0.55) * mood.sway;
  const bob = STILL ? 0 : Math.sin(phase * 0.9) * mood.sway * 0.7 + (speaking ? level * 0.7 : 0);
  head.setAttribute('transform', 'rotate(' + tilt.toFixed(2) + ' 32 40) translate(0 ' + bob.toFixed(2) + ')');
}

function frame(at) {
  level += (want - level) * 0.2;
  want *= 0.9;
  phase += STILL ? 0.004 : 0.022;
  draw();
  face(at || 0);
  requestAnimationFrame(frame);
}
function loudness(samples) {
  let sum = 0, counted = 0;
  for (let i = 0; i < samples.length; i += 4) { sum += samples[i] * samples[i]; counted++; }
  return counted ? Math.min(1, Math.sqrt(sum / counted) * 3.2) : 0;
}
window.addEventListener('resize', fit);
fit();
requestAnimationFrame(frame);

/* --- what the caller sees ------------------------------------------------------------------ */
const WORDS = {
  idle: 'Appuyez pour appeler', dialing: 'Connexion…', listening: 'Je vous écoute',
  searching: 'Il consulte la mémoire du groupe…', speaking: 'Jeli répond', ended: 'Appel terminé',
};
function setState(name) {
  stage.dataset.state = name;
  status.textContent = WORDS[name] || '';
  const live = name !== 'idle' && name !== 'ended';
  talk.setAttribute('aria-label', live ? 'Raccrocher' : 'Appeler Jeli');
}
function tell(text) { note.textContent = text; }

let open = {};
function bubble(kind) {
  const row = document.createElement('div');
  row.className = 'bubble ' + kind;
  said.hidden = false;
  said.appendChild(row);
  said.scrollTop = said.scrollHeight;
  return row;
}
function show(who, text) {
  /* The transcription arrives a few words at a time: one bubble per turn, not per fragment. */
  if (!SHOW_TRANSCRIPT || !said) return;
  const row = open[who] || (open[who] = bubble(who === 'Vous' ? 'you' : 'jeli'));
  row.textContent = (row.textContent + ' ' + text).replace(/\s+/g, ' ').trim();
  said.scrollTop = said.scrollHeight;
}
function doing(html) {
  if (!SHOW_TRANSCRIPT || !said) return null;
  const row = bubble('doing');
  row.innerHTML = html;
  return row;
}

/* --- the line ------------------------------------------------------------------------------ */
function play(bytes) {
  if (!out) return;
  const pcm = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 1);
  const buffer = out.createBuffer(1, pcm.length, 24000);
  const channel = buffer.getChannelData(0);
  for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
  const source = out.createBufferSource();
  source.buffer = buffer;
  source.connect(out.destination);
  playAt = Math.max(playAt, out.currentTime);
  source.start(playAt);
  playAt += buffer.duration;
  speakingUntil = playAt;
  if (stage.dataset.state === 'listening') setState('speaking');
  want = Math.max(want, loudness(channel));
}
setInterval(function () {
  if (out && stage.dataset.state === 'speaking' && out.currentTime > speakingUntil - 0.08) setState('listening');
}, 120);

function ask(question) {
  if (socket && socket.readyState === 1) {
    socket.send(JSON.stringify({ ask: question }));
    show('Vous', question);
    delete open['Vous'];
    return true;
  }
  return false;
}
if (chips) {
  chips.addEventListener('click', function (event) {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    if (ask(chip.dataset.ask)) return;
    pending = chip.dataset.ask;  /* not on the line yet: call first, then ask it for them */
    talk.click();
  });
}

async function start() {
  setState('dialing');
  tell('Autorisez le micro pour que Jeli vous entende.');
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  context = new AudioContext({ sampleRate: 16000 });
  out = new AudioContext({ sampleRate: 24000 });
  playAt = 0;
  socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/jeli/call/ws');
  socket.binaryType = 'arraybuffer';
  socket.onopen = function () {
    setState('listening');
    tell('Parlez normalement — vous pouvez lui couper la parole.');
    if (pending) { ask(pending); pending = null; }
  };
  socket.onmessage = function (event) {
    if (typeof event.data !== 'string') return play(new Uint8Array(event.data));
    const message = JSON.parse(event.data);
    if (message.said) show('Vous', message.said);
    if (message.jeli) show('Jeli', message.jeli);
    if (message.turn) { delete open['Vous']; delete open['Jeli']; }
    if (message.searching) {
      setState('searching');
      doing('<b>Il cherche dans la mémoire</b>');
      show('Vous', '');
    }
    if (message.searched) setState('speaking');
    if (message.state) tell(message.state);
    if (message.end) stop();
  };
  socket.onclose = function () { stop(); };
  const source = context.createMediaStreamSource(stream);
  const node = context.createScriptProcessor(2048, 1, 1);
  node.onaudioprocess = function (event) {
    if (!socket || socket.readyState !== 1) return;
    const input = event.inputBuffer.getChannelData(0);
    if (stage.dataset.state === 'listening') want = Math.max(want, loudness(input));
    const pcm = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) pcm[i] = Math.max(-1, Math.min(1, input[i])) * 32767;
    socket.send(pcm.buffer);
  };
  source.connect(node);
  node.connect(context.destination);
  mic = { stream: stream, node: node, source: source };
}

function stop() {
  setState('ended');
  tell('Rappelez quand vous voulez.');
  if (mic) {
    mic.stream.getTracks().forEach(function (track) { track.stop(); });
    mic.node.disconnect();
    mic.source.disconnect();
    mic = null;
  }
  if (context) { context.close(); context = null; }
  if (out) { out.close(); out = null; }
  if (socket && socket.readyState === 1) socket.close();
  socket = null;
  pending = null;
}

talk.onclick = async function () {
  const live = stage.dataset.state !== 'idle' && stage.dataset.state !== 'ended';
  if (live) return stop();
  try {
    await start();
  } catch (error) {
    setState('idle');
    tell("Le micro n'a pas pu s'ouvrir : " + error.message);
    pending = null;
  }
};
"""


# --- The call itself ------------------------------------------------------------------------------


@router.websocket("/jeli/call/ws")
async def call_socket(socket: WebSocket) -> None:
    await socket.accept()
    state = socket.app.state
    chosen = call_settings(state)
    llm = getattr(state, "llm", None)
    clients = getattr(llm, "_clients", None) or []
    if not chosen["on"] or not clients:
        await socket.send_text(json.dumps({"state": CALLS_CLOSED if not chosen["on"] else NO_ENGINE, "end": True}))
        await socket.close()
        return
    client: genai.Client = clients[0]
    instructions = await _instructions(state)
    line = Line(socket)
    quiet = chosen["quiet"] * 60
    watchers = [asyncio.create_task(line.listen()), asyncio.create_task(_hang_up_on_silence(line, quiet))]
    handle, model, failures = None, CALL_MODELS[0], 0
    line.air = OnAir(secrets.token_hex(4), model)
    ON_AIR[line.air.id] = line.air
    try:
        while not line.gone.is_set():
            opened = time.monotonic()
            try:
                async with client.aio.live.connect(
                    model=model, config=_config(instructions, handle, chosen["voice"])
                ) as session:
                    log.info("A call is running on %s%s", model, " (resumed)" if handle else "")
                    handle = await _talk(socket, session, state, line) or handle
                # A session that lasted is a session that worked. One that died on opening is a
                # failure however politely it closed, and must not be retried in a tight circle.
                failures = 0 if time.monotonic() - opened > SHORTEST_REAL_SESSION else failures + 1
            except WebSocketDisconnect:
                return
            except Exception:
                failures += 1
                log.warning("A call's session on %s did not open (%d in a row)", model, failures, exc_info=True)
                if failures >= GIVE_UP_AFTER:
                    await _say(socket, {"state": NO_ENGINE, "end": True})
                    return
                # Another Live model may take what this one refused: a call is worth all three.
                model = CALL_MODELS[(CALL_MODELS.index(model) + 1) % len(CALL_MODELS)]
                await asyncio.sleep(RECONNECT_PAUSE)
                continue
            if line.gone.is_set():
                break
            if failures >= GIVE_UP_AFTER:
                await _say(socket, {"state": NO_ENGINE, "end": True})
                return
            # The session ended, which Google does every few minutes. That is a reconnection, not
            # the end of the call — even the first time, before any handle has arrived.
            await _say(socket, {"state": RESUMING})
            if failures:
                await asyncio.sleep(RECONNECT_PAUSE)
        await _say(socket, {"state": GONE_QUIET if line.quiet else "Appel terminé.", "end": True})
    finally:
        ON_AIR.pop(line.air.id, None)
        await line.air.share(json.dumps({"over": True}))
        for watcher in watchers:
            watcher.cancel()
        with contextlib.suppress(Exception):
            await socket.close()


@router.websocket("/dashboard/calls/{call_id}/listen")
async def follow_socket(socket: WebSocket, call_id: str) -> None:
    """A member of the team following a call as it happens.

    Read-only in both directions that matter: nothing sent from here reaches the caller, and nothing
    heard here is written down. The caller is told on the page that the team may follow a call.
    """
    from app.web.auth import SESSION_COOKIE

    auth = getattr(socket.app.state, "auth", None)
    member = auth.member(socket.cookies.get(SESSION_COOKIE)) if auth is not None and auth.enabled else ""
    if not member:
        await socket.close(code=1008)
        return
    await socket.accept()
    call = ON_AIR.get(call_id)
    if call is None:
        await socket.send_text(json.dumps({"over": True}))
        await socket.close()
        return
    call.followers.add(socket)
    log.info("%s is following a call (%s)", member, call_id)
    try:
        for line in list(call.words):  # what was said before they joined
            await socket.send_text(json.dumps(line))
        while True:
            packet = await socket.receive()
            if packet.get("type") == "websocket.disconnect":
                break
    except Exception:
        pass
    finally:
        call.followers.discard(socket)


def _config(instructions: str, handle: str | None, voice: str = VOICE) -> types.LiveConnectConfig:
    """How every session of a call is opened.

    Two settings keep a call going. Context window compression slides the window instead of ending
    the session when it fills — measured 23 Sep: without it, the call stopped mid-conversation with
    no warning at all. Session resumption gives a handle to reopen with, so a session that does end
    is picked up where it left off rather than started again from nothing.
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=instructions,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice))
        ),
        tools=[SEARCH_TOOL],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()
        ),
        session_resumption=types.SessionResumptionConfig(handle=handle),
    )


async def _say(socket: WebSocket, note: dict) -> None:
    with contextlib.suppress(Exception):
        await socket.send_text(json.dumps(note))


# --- Calls happening right now --------------------------------------------------------------------
#
# The team can see the calls in progress and follow one live, from the dashboard. Nothing is written
# down: this is what is happening this second, and it disappears when the caller hangs up.
#
# The caller is told. The page says the team may follow a call, because a page that promises privacy
# and is listened to anyway is a page that lies — and these are the community's own members.

ON_AIR: dict[str, "OnAir"] = {}
KEPT_WORDS = 60


class OnAir:
    """One call in progress, and whoever on the team is following it."""

    def __init__(self, call_id: str, model: str):
        self.id = call_id
        self.model = model
        self.began = time.time()
        self.searches: list[str] = []
        self.words: list[dict] = []
        self.followers: set[WebSocket] = set()

    @property
    def seconds(self) -> float:
        return time.time() - self.began

    async def note(self, line: dict) -> None:
        """A line of the call: kept for whoever joins late, and passed on to those already here."""
        self.words.append(line)
        del self.words[:-KEPT_WORDS]
        await self.share(json.dumps(line))

    async def share(self, payload) -> None:
        for follower in list(self.followers):
            try:
                if isinstance(payload, bytes):
                    await follower.send_bytes(payload)
                else:
                    await follower.send_text(payload)
            except Exception:
                self.followers.discard(follower)


def calls_in_progress() -> list[dict]:
    """What the dashboard shows: the calls happening this second, longest first."""
    return sorted(
        (
            {
                "id": call.id,
                "model": call.model,
                "seconds": round(call.seconds),
                "searches": len(call.searches),
                "asked": call.searches[-1] if call.searches else "",
                "followers": len(call.followers),
            }
            for call in ON_AIR.values()
        ),
        key=lambda call: -call["seconds"],
    )


class Line:
    """The caller's side of the call: their microphone, and whether they are still on it.

    Read once for the whole call and not per session — a reconnection must not cost the socket its
    reader. And the caller leaving must end the call: measured 23 Sep, a closed tab left its Live
    session running to the stopwatch, holding one of the few slots the free tier allows, so the
    *next* person to call found none and was told Jeli could not take the call.
    """

    def __init__(self, socket: WebSocket):
        self.socket = socket
        self.audio: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.typed: asyncio.Queue = asyncio.Queue(maxsize=8)
        self.gone = asyncio.Event()
        self.quiet = False
        self.spoke_at = time.monotonic()
        self.air: OnAir | None = None  # this call, as the team sees it while it lasts

    async def listen(self) -> None:
        try:
            while True:
                packet = await self.socket.receive()
                if packet.get("type") == "websocket.disconnect":
                    break
                chunk = packet.get("bytes")
                if chunk is not None:
                    if self.audio.full():  # the line is ahead of the model: drop the oldest sound
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self.audio.get_nowait()
                    await self.audio.put(chunk)
                    if self.air is not None and self.air.followers:
                        await self.air.share(b"\x00" + chunk)  # the caller's own voice, 16 kHz
                    continue
                # A question tapped on the page instead of spoken: somebody who does not know the
                # programme has nothing to say to it, and a silent room is where a demo dies.
                question = (json.loads(packet.get("text") or "{}") or {}).get("ask", "")
                if isinstance(question, str) and question.strip():
                    self.heard()
                    with contextlib.suppress(asyncio.QueueFull):
                        self.typed.put_nowait(question.strip()[:400])
        except Exception:
            log.info("The caller hung up")
        finally:
            self.gone.set()

    def heard(self) -> None:
        self.spoke_at = time.monotonic()


async def _hang_up_on_silence(line: Line, seconds: float = QUIET_SECONDS) -> None:
    """A forgotten tab holds a microphone open for hours; a conversation does not go quiet for five
    minutes. So a call ends on silence, never on a stopwatch."""
    while not line.gone.is_set():
        quiet = time.monotonic() - line.spoke_at
        if quiet >= seconds:
            line.quiet = True
            line.gone.set()
            return
        await asyncio.sleep(min(15.0, seconds - quiet))


async def _talk(socket: WebSocket, session, state, line: Line) -> str | None:
    """One session of a call. Returns the handle to resume with, or None when there is none."""
    resume: dict[str, str | None] = {"handle": None}

    async def send() -> None:
        while True:
            chunk = await line.audio.get()
            await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))

    async def receive() -> None:
        async for message in session.receive():
            update = getattr(message, "session_resumption_update", None)
            if update is not None and getattr(update, "resumable", False) and update.new_handle:
                resume["handle"] = update.new_handle
            if getattr(message, "go_away", None) is not None:
                # Google warns seconds before closing. Reopening now, while the line is still up,
                # is the difference between a pause and a dropped call.
                log.info("The Live API asked to reconnect; reopening before it closes")
                return
            content = getattr(message, "server_content", None)
            if content is not None:
                for part in getattr(getattr(content, "model_turn", None), "parts", None) or []:
                    data = getattr(getattr(part, "inline_data", None), "data", None)
                    if data:
                        await socket.send_bytes(data)
                        if line.air is not None and line.air.followers:
                            await line.air.share(b"" + data)  # Jeli's voice, 24 kHz
                heard = getattr(getattr(content, "input_transcription", None), "text", "")
                spoken = getattr(getattr(content, "output_transcription", None), "text", "")
                if heard:
                    line.heard()  # somebody is talking: the silence watchdog starts over
                    await _say(socket, {"said": heard})
                    if line.air is not None:
                        await line.air.note({"said": heard})
                if spoken:
                    await _say(socket, {"jeli": spoken})
                    if line.air is not None:
                        await line.air.note({"jeli": spoken})
                if getattr(content, "turn_complete", False):
                    await _say(socket, {"turn": True})
                    if line.air is not None:
                        await line.air.note({"turn": True})  # the sentence is finished: close the line
            calls = getattr(getattr(message, "tool_call", None), "function_calls", None) or []
            for call in calls:
                question = (call.args or {}).get("question", "")
                log.info("A caller asked the memory: %s", question[:120])
                await _say(socket, {"searching": question})
                if line.air is not None:
                    line.air.searches.append(question)
                    await line.air.note({"searching": question})
                found = await _search(state, question)
                await session.send_tool_response(
                    function_responses=[
                        types.FunctionResponse(id=call.id, name=call.name, response={"result": found})
                    ]
                )
                await _say(socket, {"searched": True})

    async def tapped() -> None:
        """A question read off the page reaches Jeli exactly as a spoken one does."""
        while True:
            question = await line.typed.get()
            await session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=question)]), turn_complete=True
            )

    mouth = asyncio.create_task(send())
    ear = asyncio.create_task(receive())
    hand = asyncio.create_task(tapped())
    left = asyncio.create_task(line.gone.wait())  # the caller leaving ends the session with them
    done, pending = await asyncio.wait({mouth, ear, hand, left}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    for task in done:
        error = task.exception()
        if error and not isinstance(error, WebSocketDisconnect):
            log.warning("A call session ended on an error", exc_info=error)
    return resume["handle"]
