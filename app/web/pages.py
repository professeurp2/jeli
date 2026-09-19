"""The dashboard's pages: the team's control panel, in plain words.

Everything a member can change goes through Runtime (settings), the store, or an activity, and is
written to the activity log with their name. Pages show no technical detail: no model names, no
thresholds as numbers, no ids — what Jeli does, for whom, and what needs attention.
"""

import asyncio
import csv
import dataclasses
import hashlib
import io
import json
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.adapters.pacing import SlidingWindowLimiter
from app.answer.citations import display_author, ignored_keys, is_phone_number
from app.config import get_settings
from app.control.guard import INCIDENTS
from app.control.runtime import FIELDS, coerce
from app.control.schedule import WEEKDAY_NAMES, WEEKDAYS, parse_schedule
from app.control.words import KINDS, OUTCOME_WORDS, OUTCOMES, pct
from app.ingest.whatsapp_export import message_ids, parse_export, read_export_bytes
from app.kb.indexer import RECORDING_PREFIX
from app.models import Attachment, Deadline, IncomingMessage, StoredMessage
from app.web import ui
from app.web.auth import SESSION_COOKIE, allowed_change, auth_of, client_address, is_https, safe_next, signed_in
from app.web.chart import questions_chart
from app.web.ui import esc, icon, when

router = APIRouter(include_in_schema=False)

Member = Annotated[str, Depends(signed_in)]
Change = Annotated[str, Depends(allowed_change)]

WEEK = 7
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PROGRAMMES = ["", "hackathon", "Wadhwani Ignite", "MIT Universal AI", "bootcamp"]
TIMEZONES = [
    ("UTC", "GMT — Bamako, Dakar, Accra"),
    ("Africa/Lagos", "West Africa — Lagos, Kinshasa, Douala"),
    ("Africa/Kigali", "Central Africa — Kigali, Lusaka, Harare"),
    ("Africa/Nairobi", "East Africa — Kampala, Nairobi, Antananarivo"),
    ("Africa/Cairo", "Egypt — Cairo"),
    ("Africa/Johannesburg", "South Africa — Johannesburg"),
    ("Europe/London", "United Kingdom — London"),
    ("Europe/Paris", "Central Europe — Paris"),
]
# The team's cities, to read a GMT time in their own.
CITIES = [("Lagos", "Africa/Lagos"), ("Kampala", "Africa/Kampala"), ("Antananarivo", "Indian/Antananarivo")]
# How sure Jeli must be before answering, in words: similarity between the question and the best excerpt.
CARE = {"careful": 0.63, "balanced": 0.60, "relaxed": 0.57}
POINTER_CARE = {"careful": 0.75, "balanced": 0.70}


# --- Shared helpers -------------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(request: Request):
    return request.app.state


def _flash(request: Request) -> tuple[str, str] | None:
    return getattr(_state(request), "flashes", {}).pop(request.cookies.get(SESSION_COOKIE, ""), None)


def _done(request: Request, path: str, text: str, kind: str = "good") -> RedirectResponse:
    """Back to a page, with a message for the member who made the change."""
    state = _state(request)
    if not hasattr(state, "flashes"):
        state.flashes = {}
    state.flashes[request.cookies.get(SESSION_COOKIE, "")] = (kind, text)
    return RedirectResponse(path, status_code=303)


def _page(request: Request, member: str, *, title: str, subtitle: str, active: str, body: str, live: bool = False) -> HTMLResponse:
    runtime = _state(request).runtime
    session = request.cookies.get(SESSION_COOKIE, "")
    page = ui.layout(
        title=title,
        subtitle=subtitle,
        active=active,
        member=member,
        csrf=auth_of(request).csrf(session),
        body=body,
        paused=runtime.paused,
        path=request.url.path,
        flash=None if request.headers.get("x-live") else _flash(request),
        live=live,
    )
    headers = {"Cache-Control": "no-store"}
    if getattr(request.state, "marker", None):
        headers["ETag"] = request.state.marker
    return HTMLResponse(page, headers=headers)


async def _marker(request: Request) -> str:
    """Changes whenever anything a page shows may have changed."""
    state = _state(request)
    store = getattr(state, "store", None)
    whatsapp = getattr(state, "whatsapp", None)
    parts = [await store.change_marker() if store else "", str(state.runtime.paused), str(getattr(whatsapp, "status", ""))]
    parts += [f"{a.key}:{a.enabled}:{a.running}:{a.last_finished}:{a.last_ok}" for a in getattr(state, "activities", {}).values()]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:20]


async def _unchanged(request: Request) -> Response | None:
    """For a page's live check: 304 when nothing changed since the version the browser has."""
    if request.headers.get("x-live") != "1":
        return None
    request.state.marker = await _marker(request)
    if request.headers.get("if-none-match") == request.state.marker:
        return Response(status_code=304, headers={"ETag": request.state.marker})
    return None


def _csrf(request: Request) -> str:
    return auth_of(request).csrf(request.cookies.get(SESSION_COOKIE, ""))


class NoKnowledgeBase(Exception):
    """A page needs the knowledge base, which is not connected on this server."""


def _store(request: Request):
    store = getattr(_state(request), "store", None)
    if store is None:
        raise NoKnowledgeBase
    return store


async def no_knowledge_base(request: Request, error: NoKnowledgeBase) -> Response:
    member = auth_of(request).member(request.cookies.get(SESSION_COOKIE))
    if not member:
        return RedirectResponse("/login", status_code=303)
    body = ui.notice("warn", "This page needs Jeli's knowledge base, which is not connected on this server.")
    return _page(request, member, title="Not available", subtitle="The knowledge base is not connected", active="", body=body)


def _labels(request: Request) -> dict[str, str]:
    return {"team": "Team", **_state(request).runtime["chat_labels"]}


def _chat_name(chat_id: str, labels: dict[str, str]) -> str:
    if chat_id in labels:
        return labels[chat_id]
    if chat_id.startswith(RECORDING_PREFIX):
        return "a session recording"
    if chat_id.endswith("@g.us"):
        return "a WhatsApp group"
    return chat_id.replace("-", " ").capitalize()


def whatsapp_state(state) -> tuple[str, str, str]:
    """(level, headline, explanation) of Jeli's WhatsApp connection, in words."""
    whatsapp = getattr(state, "whatsapp", None)
    if whatsapp is None:
        return "neutral", "Not set up", "Jeli's server has no WhatsApp connection configured."
    status = whatsapp.status
    if status == "WORKING":
        return "good", "Connected", "Jeli reads its groups and can answer."
    if status == "SCAN_QR_CODE":
        return "warn", "Waiting for Jeli's phone", "Scan the code on the WhatsApp page with Jeli's phone to connect."
    if status == "STARTING":
        return "neutral", "Connecting…", "This usually takes a few seconds."
    return "bad", "Not connected", "Jeli cannot read or answer its groups until its phone is linked again."


def _ai_busy(state) -> bool:
    llm = getattr(state, "llm", None)
    return bool(llm) and all(resting for _, resting in llm.status())


def _ref(key: str) -> str:
    """A reference to a person for the page's buttons: their number never leaves the server."""
    return hashlib.sha256(f"jeli-person:{key}".encode()).hexdigest()[:20]


def _person(key: str, name: str = "") -> str:
    shown = display_author(key) if is_phone_number(key) else key
    if name and name.lower() != key.lower():
        return f'<span class="strong">{esc(name)}</span> <span class="muted small">{esc(shown)}</span>'
    return f'<span class="strong">{esc(shown)}</span>'


def _in_cities(at: str) -> str:
    """"18:00" GMT, read in the team's cities."""
    try:
        hours, minutes = (int(part) for part in at.split(":"))
    except ValueError:
        return ""
    moment = _now().replace(hour=hours, minute=minutes, second=0, microsecond=0)
    return " · ".join(f"{moment.astimezone(ZoneInfo(zone)):%H:%M} {city}" for city, zone in CITIES)


# --- Sign in --------------------------------------------------------------------------------------


@router.get("/")
async def home() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/dashboard") -> HTMLResponse:
    auth_of(request)
    return HTMLResponse(ui.login_page(next_path=safe_next(next)), headers={"Cache-Control": "no-store"})


@router.post("/login")
async def login(request: Request) -> Response:
    auth = auth_of(request)
    form = await request.form()
    name, password = str(form.get("name", ""))[:60].strip().lower(), str(form.get("password", ""))[:200]
    target = safe_next(str(form.get("next", "")))
    address = client_address(request)
    if auth.throttled(f"ip:{address}", f"name:{name}"):
        return HTMLResponse(ui.login_page("Too many attempts. Wait 15 minutes and try again.", target), status_code=429)
    member = await auth.verify(name, password)
    if not member:
        auth.failed(f"ip:{address}", f"name:{name}")
        return HTMLResponse(ui.login_page("That name and password don't match.", target), status_code=401)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        SESSION_COOKIE, auth.new_session(member), max_age=7 * 24 * 3600, httponly=True, secure=is_https(request), samesite="lax"
    )
    if auth.store:
        await auth.store.add_audit(member, "Signed in")
    return response


@router.post("/logout")
async def logout(request: Request, member: Change) -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/dashboard/avatar")
async def avatar(request: Request) -> Response:
    """Jeli's WhatsApp profile picture once linked, else its lemur mark."""
    state = _state(request)
    cached = getattr(state, "avatar_cache", None)
    signed = auth_of(request).member(request.cookies.get(SESSION_COOKIE)) if getattr(state, "auth", None) else None
    whatsapp = getattr(state, "whatsapp", None)
    if signed and whatsapp is not None and whatsapp.status == "WORKING":
        if not cached or time.monotonic() - cached[1] > 3600:
            me = await whatsapp.me()
            picture = await whatsapp.profile_picture(me["id"]) if me and me.get("id") else None
            state.avatar_cache = cached = (picture, time.monotonic())
        if cached[0]:
            return Response(cached[0], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    return Response(ui.LEMUR, media_type="image/svg+xml", headers={"Cache-Control": "private, max-age=600"})


# --- Pause ----------------------------------------------------------------------------------------


@router.post("/dashboard/pause")
async def pause(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    paused = form.get("action") == "pause"
    await _state(request).runtime.update({"paused": paused}, member, "Paused Jeli" if paused else "Resumed Jeli")
    text = "Jeli is paused: it answers nobody and posts nothing until you resume it." if paused else "Jeli is answering again."
    return _done(request, safe_next(str(form.get("next", ""))), text, "warn" if paused else "good")


# --- Overview -------------------------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def overview(request: Request, member: Member) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    state = _state(request)
    store = getattr(state, "store", None)
    now = _now()
    first_day = datetime.combine((now - timedelta(days=WEEK - 1)).date(), datetime.min.time(), timezone.utc)
    usage = await store.usage_since(first_day) if store else None
    upcoming = await store.deadlines_between(now.date(), now.date() + timedelta(days=WEEK)) if store else []
    watch = await store.incidents_since(now - timedelta(days=WEEK)) if store else []
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = await store.activity_today(midnight) if store else None
    recent = await store.recent_events(12) if store else []
    paused = state.runtime.paused
    level, headline, explanation = whatsapp_state(state)

    hero = (
        f'<section class="card hero{" paused" if paused else ""}"><img src="/dashboard/avatar" alt="" class="avatar">'
        f'<div class="hero-text"><h2>{"Jeli is paused" if paused else "Jeli is on duty"}</h2>'
        f'<p>{"Members get no answers and nothing is posted. Jeli keeps remembering the groups." if paused else "Members can ask it anything about the groups, the sessions and the deadlines."}</p>'
        + (
            f'<div class="today"><span>Today: <b>{today["read"]:,}</b> messages read in the groups</span>'
            f'<span><b>{today["exchanges"]:,}</b> exchanges with Jeli</span>'
            f'<span>Latest message: <b>{when(today["last_message"], "ago") if today["last_message"] else "none yet"}</b></span></div>'
            if today
            else ""
        )
        + f'</div><div class="actions">{ui.pill(level, "WhatsApp: " + headline)}</div></section>'
    )

    attention = []
    if level in ("bad", "warn"):
        attention.append(ui.notice("bad" if level == "bad" else "warn", f"{esc(explanation)} <a href='/dashboard/whatsapp'>Open the WhatsApp page</a>"))
    if _ai_busy(state):
        attention.append(ui.notice("warn", "Jeli's AI is very busy right now: for a few minutes, it replies with the sources only."))
    unanswered = [q for q in (usage or {}).get("group_questions", []) if q[1] == "dont_know"]
    if unanswered:
        attention.append(ui.notice("info", f"{len(unanswered)} question{'s' if len(unanswered) > 1 else ''} Jeli couldn't answer this week. <a href='/dashboard/questions?show=unanswered'>See them</a>"))
    if watch:
        attention.append(ui.notice("warn", f"{len(watch)} {'people' if len(watch) > 1 else 'person'} to watch for misuse this week. <a href='/dashboard/watchlist'>Review</a>"))
    for activity in getattr(state, "activities", {}).values():
        if activity.last_ok is False:
            attention.append(ui.notice("warn", f"{esc(activity.name)} had a problem {when(activity.last_finished, 'ago')}. <a href='/dashboard/activities'>Activities</a>"))
    soon = [d for d in upcoming if d.due_date <= now.date() + timedelta(days=2)]
    if soon:
        attention.append(ui.notice("info", f"{len(soon)} deadline{'s' if len(soon) > 1 else ''} in the next 3 days. <a href='/dashboard/deadlines'>Deadlines</a>"))
    if not attention:
        attention.append(ui.notice("good", "Everything is running smoothly."))

    if usage is None:
        usage_html = ui.card("This week", ui.empty("Connect the knowledge base to see how Jeli is used."), icon_name="activity")
    else:
        days = [(now - timedelta(days=WEEK - 1 - i)).date() for i in range(WEEK)]
        counts = {(day, outcome): n for day, outcome, n in usage["questions_by_day"]}
        by_outcome = {o: sum(n for (d, oc), n in counts.items() if oc == o) for o, _ in OUTCOMES}
        questions = usage["by_kind"].get("question", 0)
        typical = f"{usage['median_ms'] / 1000:.0f} s" if usage["median_ms"] is not None else "—"
        stats = "".join(
            [
                ui.stat("Questions asked", f"{questions:,}", "last 7 days"),
                ui.stat("Answered with sources", pct(by_outcome["answered"], questions), f"{by_outcome['answered']:,} questions"),
                ui.stat("Couldn't answer", pct(by_outcome["dont_know"], questions), f"{by_outcome['dont_know']:,} questions"),
                ui.stat("Typical reply time", typical, "from question to answer"),
            ]
        )
        legend = "".join(f'<li><span class="swatch s{i}"></span>{esc(label)}</li>' for i, (_, label) in enumerate(OUTCOMES, 1))
        table_rows = [
            [f"{d:%a %d %b}", *[str(counts.get((d, o), 0)) for o, _ in OUTCOMES], str(sum(counts.get((d, o), 0) for o, _ in OUTCOMES))]
            for d in days
        ]
        others = " · ".join(f"{esc(label)} <b>{usage['by_kind'].get(kind, 0)}</b>" for kind, label in KINDS)
        usage_html = (
            f'<div class="stats">{stats}</div>'
            + ui.card(
                "Questions per day",
                f'<ul class="legend">{legend}</ul><div class="chart-wrap">{questions_chart(days, counts)}</div>'
                f"<details><summary>Show as a table</summary>"
                + ui.table(["Day", *[label for _, label in OUTCOMES], "Total"], table_rows, numeric={1, 2, 3, 4})
                + f'</details><p class="hint" style="margin-top:12px">Also this week: {others}</p>',
                icon_name="activity",
                description="How the questions members asked Jeli went, day by day.",
            )
        )
        usage_html = f'<span id="chart-title" hidden>Questions per day, by outcome</span>{usage_html}'

    labels = _labels(request)
    coming = (
        "".join(
            f'<div class="row"><div class="row-text"><b>{esc(d.what)}</b><span>{d.due_date:%a %d %b}'
            f'{", " + esc(d.due_time) if d.due_time else ""} · {esc(_chat_name(d.chat_id, labels))}</span></div></div>'
            for d in upcoming[:6]
        )
        if upcoming
        else ui.empty("Nothing due in the next 7 days.", "calendar")
    )
    activities = "".join(
        f'<div class="row"><div class="row-text"><b>{esc(a.name)}</b><span>{_activity_line(a)}</span></div>'
        f'<div class="row-side">{_activity_pill(a)}</div></div>'
        for a in getattr(state, "activities", {}).values()
    ) or ui.empty("No background activity.")
    feed = ui.card(
        "Live conversations",
        _feed(recent, labels),
        icon_name="chat",
        description="The latest exchanges with Jeli, as they happen — in the groups, in private, and your tries.",
    )
    side = ui.card("Coming up", f'<div class="rows">{coming}</div>', icon_name="calendar", actions="<a class='btn small' href='/dashboard/deadlines'>All deadlines</a>") + ui.card(
        "Activities", f'<div class="rows">{activities}</div>', icon_name="activity", actions="<a class='btn small' href='/dashboard/activities'>Manage</a>"
    )
    body = (
        hero
        + f'<div class="attention">{"".join(attention)}</div>'
        + f'<div class="grid side"><div class="grid">{feed}{usage_html}</div><div class="grid">{side}</div></div>'
    )
    return _page(request, member, title="Overview", subtitle="How Jeli is doing, at a glance", active="home", body=body, live=True)


FEED_KINDS = {
    "question": ("question", "Question"),
    "catchup": ("activity", "Catch-up"),
    "recap": ("book", "Session recap"),
    "deadlines": ("calendar", "Deadlines"),
    "search": ("list", "Search"),
    "already_answered": ("sparkle", "Pointed to an earlier answer"),
    "social": ("chat", "Chat"),
    "help": ("info", "Help"),
    "file": ("download", "File"),
}
FEED_OUTCOMES = {"answered": ("good", "Answered"), "dont_know": ("warn", "Couldn't answer"), "sources_only": ("info", "Sources only"), "not_ready": ("neutral", "Not ready")}


def _feed(events: list[dict], labels: dict[str, str]) -> str:
    if not events:
        return ui.empty("No exchange yet. They appear here the moment members talk to Jeli — try it on the Try Jeli page.", "chat")
    items = []
    for event in events:
        glyph, kind = FEED_KINDS.get(event["kind"], ("chat", event["kind"].capitalize()))
        test = event["channel"] == "dashboard"
        if test:
            where = "Try on the dashboard"
        elif event["is_private"]:
            where = "Private chat"
        else:
            where = _chat_name(event["chat_id"], labels) if event["chat_id"] else "A group"
        text = esc(event["question"]) if event["question"] else f'<span class="muted">{"A private message" if event["is_private"] else esc(kind)}</span>'
        outcome = FEED_OUTCOMES.get(event["outcome"])
        pill = ui.pill(*outcome) if outcome else ""
        speed = f'<span class="muted small">{event["latency_ms"] / 1000:.1f} s</span>' if event["latency_ms"] else ""
        items.append(
            f'<div class="feed-item"><span class="feed-icon{" test" if test else ""}">{icon(glyph, 15)}</span>'
            f'<div><div class="feed-text">{text}</div><div class="feed-where">{esc(kind)} · {esc(where)} · {when(event["at"], "ago")}</div></div>'
            f'<div class="actions">{pill}{speed}</div></div>'
        )
    return f'<div class="feed">{"".join(items)}</div>'


def _activity_pill(activity) -> str:
    if activity.running:
        return ui.pill("info", "Running now")
    if not activity.enabled:
        return ui.pill("neutral", "Off")
    if activity.blocked():
        return ui.pill("warn", "Waiting")
    if activity.last_ok is False:
        return ui.pill("bad", "Had a problem")
    return ui.pill("good", "On")


def _activity_line(activity) -> str:
    if activity.running:
        return f"Started {when(activity.last_started, 'ago')}"
    if not activity.enabled:
        return "Switched off"
    if reason := activity.blocked():
        return esc(reason)
    if activity.next_at:
        return f"Next {when(activity.next_at, 'ago')}"
    return ""


# --- Try Jeli -------------------------------------------------------------------------------------

EXAMPLES = [
    "When is the hackathon submission deadline?",
    "What happened today in the group?",
    "/deadlines",
    "/recap module 1",
    "Who are you?",
]
PRIVATE = "private"


def _try_chats(labels: dict[str, str], chats: list[dict]) -> list[tuple[str, str]]:
    """Where the tester writes from: a group Jeli knows, or a private chat with Jeli."""
    return [(c["chat_id"], _chat_name(c["chat_id"], labels)) for c in chats] + [(PRIVATE, "Private chat with Jeli")]


def _try_entry(row: dict) -> dict:
    details = row["details"] or {}
    role = "system" if details.get("system") else row["role"]
    return {**details, "role": role, "text": row["text"], "at": row["at"].isoformat()}


@router.get("/dashboard/try", response_class=HTMLResponse)
async def try_page(request: Request, member: Member) -> HTMLResponse:
    store = getattr(_state(request), "store", None)
    history = [_try_entry(row) for row in await store.tries(member)] if store else []
    chats = (await store.knowledge_overview())["chats"] if store else []
    options = "".join(f'<option value="{esc(value)}">{esc(name)}</option>' for value, name in _try_chats(_labels(request), chats))
    chips = "".join(f'<button type="button" class="chip" data-example="{esc(e)}">{esc(e)}</button>' for e in EXAMPLES)
    clear = ui.form(
        "/dashboard/try/clear",
        _csrf(request),
        ui.button("New conversation", kind="small", icon_name="plus"),
        confirm="Start a new conversation? This one is deleted from your history.",
        cls="inline",
    )
    body = ui.card(
        "Try Jeli",
        f"""<div class="wa">
          <div class="wa-head"><img src="/dashboard/avatar" class="wa-avatar" alt="">
            <div class="wa-who"><b>Jeli</b><span id="wa-status">online</span></div>
            <label class="wa-where">Writing in<select id="chat">{options}</select></label></div>
          <div class="wa-chat" id="chat-log" aria-live="polite"></div>
          <form class="wa-compose" id="composer">
            <div class="chips">{chips}</div>
            <div class="segmented" role="radiogroup" aria-label="How the message is sent">
              <label><input type="radio" name="mode" value="ask" checked><span>Calling Jeli</span></label>
              <label><input type="radio" name="mode" value="group"><span>Without calling Jeli</span></label>
            </div>
            <div class="wa-row"><textarea name="text" id="text" placeholder="Type a message" required maxlength="2000" rows="1"></textarea>
            <button type="submit" class="wa-send" aria-label="Send">{icon("send", 20)}</button></div>
            <p class="hint">Shown exactly as on WhatsApp. Nothing is sent there, and Jeli answers here even while paused.
            “Without calling Jeli” shows whether it would step in on its own, or follow the conversation.</p>
          </form>
        </div>""",
        icon_name="chat",
        description="Your conversation with Jeli is kept, for you only. Every try also appears on the Overview.",
        actions=clear,
    )
    script = f"""<script>
document.addEventListener('DOMContentLoaded', function () {{
  const log = document.getElementById('chat-log'), form = document.getElementById('composer'), text = document.getElementById('text');
  const chat = document.getElementById('chat'), status = document.getElementById('wa-status');
  const csrf = {_js(_csrf(request))}, me = {_js(member.capitalize())};
  try {{ const saved = localStorage.getItem('jeli-try-chat'); if (saved && [...chat.options].some(o => o.value === saved)) chat.value = saved; }} catch (e) {{}}
  chat.addEventListener('change', () => {{ try {{ localStorage.setItem('jeli-try-chat', chat.value); }} catch (e) {{}} }});
  function one(entry) {{ log.appendChild(window.jeliBubble(entry, me)); log.scrollTop = log.scrollHeight; }}
  function show(entry) {{
    one(entry);
    (entry.files || []).forEach(file => one({{ role: 'jeli', file, at: entry.at }}));
    if (entry.after) one({{ role: 'jeli', text: entry.after, at: entry.at }});
  }}
  {_data(history)}.forEach(show);
  if (!log.children.length) show({{ role: 'jeli', text: 'Hello ' + me + '! 👋 Write to me as a member would. I answer here exactly as I would on WhatsApp.', at: new Date().toISOString() }});
  document.querySelectorAll('[data-example]').forEach(c => c.addEventListener('click', () => {{ text.value = c.dataset.example; text.focus(); }}));
  text.addEventListener('keydown', e => {{ if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); form.requestSubmit(); }} }});
  form.addEventListener('submit', async e => {{
    e.preventDefault();
    const message = text.value.trim(); if (!message) return;
    const mode = form.querySelector('input[name=mode]:checked').value;
    text.value = '';
    show({{ role: 'member', text: message, at: new Date().toISOString(), called: mode === 'ask' }});
    status.textContent = 'typing…';
    try {{
      const r = await fetch('/dashboard/try', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }},
        body: JSON.stringify({{ text: message, mode, chat: chat.value }}) }});
      const data = await r.json();
      if (!r.ok) show({{ role: 'system', text: data.error || 'Something went wrong.' }});
      else show(data.jeli);
    }} catch (err) {{ show({{ role: 'system', text: 'Jeli could not be reached. Try again.' }}); }}
    status.textContent = 'online';
  }});
}});
</script>"""
    return _page(request, member, title="Try Jeli", subtitle="Talk to Jeli as a member would, exactly as on WhatsApp", active="try", body=body + script)


def _js(value) -> str:
    return _data(value)


def _data(value) -> str:
    """A value for a <script>: JSON that cannot close the script tag."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@router.post("/dashboard/try")
async def try_ask(request: Request, member: Change) -> JSONResponse:
    state = _state(request)
    limiter = getattr(state, "try_limiter", None)
    if limiter is None:
        limiter = state.try_limiter = SlidingWindowLimiter(30, 600)
    if not limiter.allow(member):
        return JSONResponse({"error": "That's a lot of tries: wait a few minutes (it saves Jeli's AI for the members)."}, status_code=429)
    data = await request.json()
    text = " ".join(str(data.get("text", "")).split())[:2000]
    if not text:
        return JSONResponse({"error": "Type a message first."}, status_code=400)
    chat = str(data.get("chat", PRIVATE))
    private = chat == PRIVATE
    called = data.get("mode") != "group" or private
    message = IncomingMessage(
        platform="dashboard",
        chat_id=f"dashboard:{member}" if private else chat,
        message_id=secrets.token_hex(8),
        author=member.capitalize(),
        text=text,
        sent_at=_now(),
        is_private=private,
        addressed_to_bot=called,
    )
    followed = not called and state.responder.is_follow_up(message)
    if followed:
        message = dataclasses.replace(message, addressed_to_bot=True)
    started = time.monotonic()
    reply = await state.responder.respond(message)
    files, after = [], None
    attachment, pending = getattr(reply, "attachment", None), getattr(reply, "pending", None)
    if pending:
        try:
            result = await asyncio.wait_for(pending(), timeout=170)
        except Exception:
            result = None
        if isinstance(result, Attachment):
            attachment = result
        elif result:
            after = result
    if attachment and attachment.document_id:
        files.append({"name": attachment.filename, "size": _size(len(attachment.data)),
                      "url": f"/dashboard/documents/download?id={quote(attachment.document_id)}"})
    seconds = round(time.monotonic() - started, 1)
    if reply:
        quoted = getattr(reply, "quoted", None) or ("You", text)
        jeli = {"role": "jeli", "text": str(reply), "at": _now().isoformat(), "quoted": list(quoted), "seconds": seconds}
        if getattr(reply, "reply_to", None):
            jeli["note"] = "Replies to the source message itself: WhatsApp shows it above, a tap jumps to it."
        if followed:
            jeli["note"] = "Followed the conversation: no need to call Jeli again."
        if files:
            jeli["files"] = files
        if after:
            jeli["after"] = after
    else:
        note = (
            "Jeli stays silent: the group hasn't answered this before, and you are not in a conversation with it."
            if not called
            else "Jeli has nothing to say to this."
        )
        jeli = {"role": "system", "text": note, "at": _now().isoformat()}
    store = getattr(state, "store", None)
    if store:
        await store.add_try(member, "member", text, {"called": called, "chat": chat})
        await store.add_try(member, "jeli", jeli["text"], {k: v for k, v in jeli.items() if k not in ("role", "text", "at")} | {"system": jeli["role"] == "system"})
    return JSONResponse({"jeli": jeli})


@router.post("/dashboard/try/clear")
async def try_clear(request: Request, member: Change) -> RedirectResponse:
    store = getattr(_state(request), "store", None)
    if store:
        await store.clear_tries(member)
    _state(request).responder.conversations.forget_member(member.capitalize())
    return _done(request, "/dashboard/try", "New conversation started.")


# --- Questions ------------------------------------------------------------------------------------


@router.get("/dashboard/questions", response_class=HTMLResponse)
async def questions_page(request: Request, member: Member, show: str = "all", days: int = 7) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    days = 30 if days == 30 else 7
    asked = await _store(request).questions_since(_now() - timedelta(days=days))
    unanswered = [q for q in asked if q[1] == "dont_know"]
    listed = unanswered if show == "unanswered" else asked
    tone = {"answered": "good", "dont_know": "warn", "sources_only": "info", "not_ready": "neutral"}

    def tab(label: str, target_show: str, target_days: int, current: bool) -> str:
        return f'<a class="btn small{" primary" if current else ""}" href="/dashboard/questions?show={target_show}&days={target_days}">{esc(label)}</a>'

    filters = (
        f'<div class="actions">{tab(f"All ({len(asked)})", "all", days, show != "unanswered")}'
        f'{tab(f"Couldn’t answer ({len(unanswered)})", "unanswered", days, show == "unanswered")}'
        f'<span class="muted small" style="margin-left:8px">Period</span>{tab("7 days", show, 7, days == 7)}{tab("30 days", show, 30, days == 30)}</div>'
    )
    rows = [[when(at), esc(question), ui.pill(tone.get(outcome, "neutral"), OUTCOME_WORDS.get(outcome, outcome))] for at, outcome, question in listed]
    plain = "\n".join(f"- {question}" for _, _, question in listed)
    actions = (
        ui.button("Copy as text", kind="small", icon_name="copy", type_="button", attrs='data-copy="plain-questions"')
        + f'<a class="btn small" href="/dashboard/questions.csv?days={days}">{icon("download", 16)}<span>Download</span></a>'
    )
    body = ui.card(
        "What members asked Jeli in the groups",
        filters
        + '<div style="height:14px"></div>'
        + ui.table(["Asked", "Question", "Outcome"], rows, empty_text="No questions in this period yet.")
        + f'<pre id="plain-questions" hidden>{esc(plain)}</pre>',
        icon_name="question",
        description="Never who asked. Questions sent to Jeli in private are only counted. Use them for the FAQ, the pitch and the next features.",
        actions=actions,
    )
    if show == "unanswered":
        body = ui.notice("info", "These are Jeli's knowledge gaps: answer them in the group, or add the conversation or session that does on the Knowledge page.") + body
    return _page(request, member, title="Questions", subtitle="What the community asks, and what Jeli couldn't answer", active="questions", body=body, live=True)


@router.get("/dashboard/questions.csv")
async def questions_csv(request: Request, member: Member, days: int = 30) -> Response:
    asked = await _store(request).questions_since(_now() - timedelta(days=30 if days == 30 else 7))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["asked (UTC)", "outcome", "question"])
    for at, outcome, question in asked:
        # A cell starting with = + - @ would run as a formula in a spreadsheet.
        safe = "'" + question if question[:1] in ("=", "+", "-", "@") else question
        writer.writerow([f"{at:%Y-%m-%d %H:%M}", OUTCOME_WORDS.get(outcome, outcome), safe])
    return Response(
        out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=jeli-questions.csv", "Cache-Control": "no-store"},
    )


# --- Deadlines ------------------------------------------------------------------------------------


@router.get("/dashboard/deadlines", response_class=HTMLResponse)
async def deadlines_page(request: Request, member: Member) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    today = _now().date()
    items = await _store(request).deadlines_between(today - timedelta(days=14), today + timedelta(days=180))
    labels, csrf = _labels(request), _csrf(request)

    def rows(deadlines: list[Deadline]) -> list[list[str]]:
        return [
            [
                f'<span class="strong nowrap">{d.due_date:%a %d %b}</span><br><span class="muted small">{esc(d.due_time)}</span>',
                esc(d.what) + (f'<br>{ui.pill("neutral", d.programme)}' if d.programme else ""),
                f'<span class="small">{esc(display_author(d.author) or "—")}<br><span class="muted">in {esc(_chat_name(d.chat_id, labels))}, {d.announced_at:%d %b}</span></span>',
                ui.form(
                    "/dashboard/deadlines/remove",
                    csrf,
                    ui.hidden("id", str(d.id)) + ui.button("Remove", kind="small danger", icon_name="trash"),
                    confirm=f"Remove “{d.what}”? It disappears from every list and won't be found again.",
                    cls="inline",
                ),
            ]
            for d in deadlines
        ]

    upcoming = [d for d in items if d.due_date >= today]
    past = [d for d in items if d.due_date < today]
    options = "".join(f'<option value="{esc(p)}">{esc(p or "No particular programme")}</option>' for p in PROGRAMMES)
    add = ui.form(
        "/dashboard/deadlines/add",
        csrf,
        f"""<div class="fields four">
          <label>What is due<input name="what" required maxlength="200" placeholder="Hackathon: submit the chatbot"></label>
          <label>Day<input name="due_date" type="date" required min="{today:%Y-%m-%d}"></label>
          <label>Time (optional)<input name="due_time" maxlength="40" placeholder="9:00 CAT"></label>
          <label>Programme<select name="programme">{options}</select></label>
        </div><div class="actions" style="margin-top:14px">{ui.button("Add the deadline", kind="primary", icon_name="plus")}</div>""",
    )
    body = (
        ui.card(
            "Coming up",
            ui.table(["Due", "What", "Announced by", ""], rows(upcoming), empty_text="No deadline found yet."),
            icon_name="calendar",
            description="Found by Jeli in the groups and sessions, or added by the team. Members see them with /deadlines and in the summaries.",
        )
        + ui.card("Add a deadline", add, icon_name="plus", description="For a deadline announced elsewhere (e-mail, website).")
        + ui.card("Past two weeks", ui.table(["Due", "What", "Announced by", ""], rows(past), empty_text="Nothing in the past two weeks."), icon_name="clock")
    )
    return _page(request, member, title="Deadlines", subtitle="What members must not miss", active="deadlines", body=body, live=True)


@router.post("/dashboard/deadlines/add")
async def deadlines_add(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    what = " ".join(str(form.get("what", "")).split())[:200]
    try:
        due = date.fromisoformat(str(form.get("due_date", "")))
    except ValueError:
        return _done(request, "/dashboard/deadlines", "Choose the day it is due.", "bad")
    programme = str(form.get("programme", ""))
    if not what or programme not in PROGRAMMES:
        return _done(request, "/dashboard/deadlines", "Say what is due.", "bad")
    store = _store(request)
    deadline = Deadline(
        what=what, due_date=due, chat_id="team", announced_at=_now(),
        due_time=" ".join(str(form.get("due_time", "")).split())[:40], programme=programme, author=member.capitalize(),
    )
    if not await store.add_deadlines([deadline]):
        return _done(request, "/dashboard/deadlines", "This deadline is already known.", "info")
    await store.add_audit(member, f"Added the deadline “{what}” ({due:%a %d %b})")
    return _done(request, "/dashboard/deadlines", f"Added “{what}”.")


@router.post("/dashboard/deadlines/remove")
async def deadlines_remove(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    store = _store(request)
    try:
        what = await store.dismiss_deadline(int(str(form.get("id", ""))), member)
    except ValueError:
        what = None
    if not what:
        return _done(request, "/dashboard/deadlines", "That deadline was already removed.", "info")
    await store.add_audit(member, f"Removed the deadline “{what}”")
    return _done(request, "/dashboard/deadlines", f"Removed “{what}”.")


# --- Knowledge ------------------------------------------------------------------------------------


@router.get("/dashboard/knowledge", response_class=HTMLResponse)
async def knowledge_page(request: Request, member: Member, preview: str = "") -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    store = _store(request)
    overview = await store.knowledge_overview()
    labels, csrf = _labels(request), _csrf(request)
    chats = overview["chats"]
    recordings = overview["recordings"]
    hours = sum((r["duration_seconds"] or 0) for r in recordings) / 3600
    stats = "".join(
        [
            ui.stat("Messages remembered", f"{sum(c['messages'] for c in chats):,}", "from the groups"),
            ui.stat("Conversations", str(len(chats)), "groups and chat histories"),
            ui.stat("Sessions", str(len(recordings)), "recorded calls, transcribed"),
            ui.stat("Hours of sessions", f"{hours:.1f}", "searchable to the minute"),
        ]
    )
    chat_rows = [
        [
            ui.form(
                "/dashboard/knowledge/rename",
                csrf,
                ui.hidden("chat", c["chat_id"])
                + f'<input name="label" value="{esc(_chat_name(c["chat_id"], labels))}" maxlength="80" aria-label="Name shown in sources" style="min-width:180px">'
                + ui.button("Save", kind="small"),
                cls="inline actions",
            ),
            f"{c['messages']:,}",
            when(c["last_message"], "date"),
            ui.pill("good", "Live") if c["live"] else ui.pill("neutral", "History"),
        ]
        for c in chats
    ]
    languages = {"en": "English", "fr": "French"}
    session_rows = [
        [
            f'<span class="strong">{esc(r["title"])}</span>',
            when(r["recorded_at"], "date"),
            ui.duration(r["duration_seconds"]),
            esc(", ".join(languages.get(k, k) for k in r["recaps"]) or "—"),
        ]
        for r in recordings
    ]
    pending = getattr(_state(request), "pending_imports", {}).get(preview)
    preview_html = ""
    if pending and pending["member"] == member:
        preview_html = ui.card(
            "Check before adding",
            f"""<p>“{esc(pending['filename'])}”: <b>{pending['count']:,} messages</b> from <b>{pending['people']} people</b>,
            from {pending['first']:%d %b %Y} to {pending['last']:%d %b %Y}, into <b>{esc(pending['label'])}</b>.</p>
            <p class="hint">Messages Jeli already knows are skipped. Check that the first and last dates look right: if not, the phone's time zone or date format was wrong.</p>
            <div class="actions" style="margin-top:12px">"""
            + ui.form("/dashboard/knowledge/import", csrf, ui.hidden("token", preview) + ui.button("Add to Jeli's knowledge", kind="primary", icon_name="check"), cls="inline")
            + '<a class="btn" href="/dashboard/knowledge">Cancel</a></div>',
            icon_name="upload",
            cls="",
        )
    chat_options = "".join(f'<option value="{esc(c["chat_id"])}">{esc(_chat_name(c["chat_id"], labels))}</option>' for c in chats)
    zone_default = get_settings().export_timezone
    zones = "".join(f'<option value="{z}"{" selected" if z == zone_default else ""}>{esc(label)}</option>' for z, label in TIMEZONES)
    upload = ui.form(
        "/dashboard/knowledge/upload",
        csrf,
        f"""<div class="fields two">
          <label>Chat file from WhatsApp (.txt or .zip)<input type="file" name="file" accept=".txt,.zip" required></label>
          <label>Add it to<select name="chat"><option value="">A new conversation (name it below)</option>{chat_options}</select></label>
          <label>Name of a new conversation<input name="label" maxlength="80" placeholder="METI cohort — before Jeli joined"></label>
          <label>Time zone of the phone that exported it<select name="timezone">{zones}</select></label>
        </div>
        <label class="check" style="margin-top:12px"><input type="checkbox" name="month_first"> Dates are written month first (e.g. 9/24/26, American phones)</label>
        <p class="hint" style="margin-top:8px">On the phone: open the group → ⋮ or the group name → More → Export chat → Without media. You'll see a summary before anything is added.</p>
        <div class="actions" style="margin-top:14px">{ui.button("Read the file", kind="primary", icon_name="upload")}</div>""",
        upload=True,
    )
    documents = await store.list_documents() if hasattr(store, "list_documents") else []
    languages_long = {"en": "English", "fr": "French"}
    document_rows = [
        [
            f'<span class="strong">{esc(d.title)}</span><br><span class="muted small">{esc(d.filename)} · {_size(d.size_bytes)}</span>',
            str(d.pages),
            esc(languages_long.get(d.language, d.language)),
            f'<span class="small">{esc(display_author(d.shared_by))}<br><span class="muted">{esc(_chat_name(d.chat_id, labels)) if d.chat_id else "on the dashboard"}, {d.shared_at:%d %b}</span></span>',
            '<div class="actions">'
            + f'<a class="btn small" href="/dashboard/documents/download?id={esc(d.id)}">{icon("download", 16)}<span>Download</span></a>'
            + ui.form("/dashboard/documents/remove", csrf, ui.hidden("id", d.id) + ui.button("Remove", kind="small danger", icon_name="trash"),
                      confirm=f"Remove “{d.title}”? Jeli forgets it and can no longer send it.", cls="inline")
            + "</div>",
        ]
        for d in documents
    ]
    add_document = ui.form(
        "/dashboard/documents/upload",
        csrf,
        f"""<div class="fields two">
          <label>Document (PDF, Word or text, 15 MB at most)<input type="file" name="file" accept=".pdf,.docx,.txt,.md" required></label>
          <label>Title (optional)<input name="title" maxlength="120" placeholder="Hackathon guidelines"></label>
        </div><div class="actions" style="margin-top:14px">{ui.button("Add the document", kind="primary", icon_name="upload")}</div>""",
        upload=True,
    )
    sessions_state = getattr(_state(request), "sessions", None)
    jobs = sorted(sessions_state.imports.values(), key=lambda j: j.started_at, reverse=True) if sessions_state else []
    state_pills = {"waiting": ("neutral", "Waiting"), "transcribing": ("info", "Transcribing"), "learning": ("info", "Learning"),
                   "done": ("good", "Added"), "failed": ("bad", "Failed")}
    job_rows = [[f'<span class="strong">{esc(j.title)}</span><br><span class="muted small">{esc(j.url)}</span>', ui.pill(*state_pills[j.state]),
                 esc(j.progress), when(j.started_at, "ago")] for j in jobs[:8]]
    add_session = ui.form(
        "/dashboard/sessions/add",
        csrf,
        f"""<div class="fields four">
          <label>YouTube link of the recording<input name="url" type="url" required placeholder="https://youtu.be/…"></label>
          <label>Title<input name="title" required maxlength="120" placeholder="Wadhwani Ignite — Module 2 class"></label>
          <label>Day of the session<input name="day" type="date" required></label>
          <label>Start (GMT)<input name="at" type="time" value="13:00"></label>
        </div>
        <p class="hint" style="margin-top:8px">Jeli watches the video and transcribes it — about ten minutes for an hour of video — then members can ask about it and get a link to the exact moment. A Teams or Zoom recording must first be posted on YouTube (unlisted is fine). Recordings shared in the groups as a YouTube link are added on their own (Settings).</p>
        <div class="actions" style="margin-top:14px">{ui.button("Add the session", kind="primary", icon_name="plus")}</div>""",
    )
    session_rows = [
        row + [ui.form("/dashboard/sessions/remove", csrf, ui.hidden("id", r["id"]) + ui.button("Remove", kind="small danger", icon_name="trash"),
                       confirm=f"Remove the session “{r['title']}”? Jeli forgets what was said in it.", cls="inline") if r.get("id") else ""]
        for row, r in zip(session_rows, recordings)
    ]
    body = (
        preview_html
        + f'<div class="stats">{stats}</div>'
        + ui.card("Documents", ui.table(["Document", "Pages", "Language", "Shared by", ""], document_rows, numeric={1},
                                        empty_text="No document yet. Documents shared in the groups are kept on their own."),
                  icon_name="book", description="Jeli answers from them page by page, and sends them — translated if asked — when members ask for them.")
        + ui.card("Add a document", add_document, icon_name="upload", description="Guidelines, rules, forms: anything members may ask about or ask for.")
        + ui.card("Sessions", ui.table(["Session", "Date", "Length", "Summaries", ""], session_rows, empty_text="No session yet."),
                  icon_name="clock", description="Recorded calls Jeli can quote to the minute.")
        + ui.card("Add a recorded session", add_session + (ui.table(["Session", "State", "Progress", "Started"], job_rows) if job_rows else ""),
                  icon_name="plus", description="From its YouTube link.")
        + ui.card("Add old conversations", upload, icon_name="upload", description="Give Jeli a group's history from before it joined, from WhatsApp's “Export chat”.")
        + ui.card("Conversations", ui.table(["Name shown in sources", "Messages", "Latest", ""], chat_rows, numeric={1}, empty_text="No conversation yet."), icon_name="chat", description="Rename a conversation to change how Jeli cites it.")
    )
    return _page(request, member, title="Knowledge", subtitle="What Jeli remembers and can quote", active="knowledge", body=body, live=True)


def _size(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


@router.post("/dashboard/documents/upload")
async def documents_upload(request: Request, member: Change) -> RedirectResponse:
    documents = getattr(_state(request), "documents", None)
    if documents is None:
        return _done(request, "/dashboard/knowledge", "Documents need the knowledge base and Jeli's AI.", "bad")
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _done(request, "/dashboard/knowledge", "Choose the document first.", "bad")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    try:
        document, new = await documents.add(
            upload.filename, data, title=str(form.get("title", "")), shared_by=member.capitalize(), mimetype=upload.content_type or ""
        )
    except ValueError as error:
        return _done(request, "/dashboard/knowledge", f"This document cannot be added: {error}.", "bad")
    if not new:
        return _done(request, "/dashboard/knowledge", f"“{document.title}” is already known.", "info")
    await _store(request).add_audit(member, f"Added the document “{document.title}”")
    memory = getattr(_state(request), "activities", {}).get("memory")
    if memory:
        memory.run_now(member)
    return _done(request, "/dashboard/knowledge", f"“{document.title}” added ({document.pages} pages). Jeli is learning it now.")


@router.get("/dashboard/documents/download")
async def documents_download(request: Request, member: Member, id: str) -> Response:
    store = _store(request)
    document = (await store.documents([id])).get(id)
    data = await store.document_content(id) if document else None
    if not document or data is None:
        raise HTTPException(status_code=404)
    safe = re.sub(r'[^\w .()-]+', "_", document.filename)[:150] or "document"
    return Response(data, media_type=document.mimetype, headers={"Content-Disposition": f'attachment; filename="{safe}"', "Cache-Control": "no-store"})


@router.post("/dashboard/documents/remove")
async def documents_remove(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    store = _store(request)
    document_id = str(form.get("id", ""))
    document = (await store.documents([document_id])).get(document_id)
    if not document:
        return _done(request, "/dashboard/knowledge", "That document was already removed.", "info")
    await store.remove_document(document_id)
    await store.add_audit(member, f"Removed the document “{document.title}”")
    return _done(request, "/dashboard/knowledge", f"Removed “{document.title}”.")


@router.post("/dashboard/sessions/add")
async def sessions_add(request: Request, member: Change) -> RedirectResponse:
    sessions = getattr(_state(request), "sessions", None)
    if sessions is None:
        return _done(request, "/dashboard/knowledge", "Sessions need the knowledge base and Jeli's AI.", "bad")
    form = await request.form()
    try:
        day = date.fromisoformat(str(form.get("day", "")))
        hours, minutes = (int(x) for x in str(form.get("at") or "13:00").split(":")[:2])
        recorded_at = datetime(day.year, day.month, day.day, hours, minutes, tzinfo=timezone.utc)
        job = sessions.start(str(form.get("url", "")).strip(), str(form.get("title", "")), recorded_at, member.capitalize())
    except ValueError as error:
        return _done(request, "/dashboard/knowledge", f"This session cannot be added: {error}.", "bad")
    if job is None:
        return _done(request, "/dashboard/knowledge", "This session is already being added.", "info")
    await _store(request).add_audit(member, f"Started adding the session “{job.title}”")
    return _done(request, "/dashboard/knowledge", f"Jeli is transcribing “{job.title}”. Follow its progress below.")


@router.post("/dashboard/sessions/remove")
async def sessions_remove(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    recording_id = str(form.get("id", ""))
    if not recording_id.startswith(RECORDING_PREFIX):
        return _done(request, "/dashboard/knowledge", "Unknown session.", "bad")
    store = _store(request)
    recording = (await store.recordings([recording_id])).get(recording_id)
    await store.forget(recording_id)
    await store.add_audit(member, f"Removed the session “{recording.title if recording else recording_id}”")
    return _done(request, "/dashboard/knowledge", "Session removed.")


@router.post("/dashboard/knowledge/upload")
async def knowledge_upload(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return _done(request, "/dashboard/knowledge", "Choose the chat file first.", "bad")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return _done(request, "/dashboard/knowledge", "This file is too big (25 MB at most). Export the chat without media.", "bad")
    zone = str(form.get("timezone", "UTC"))
    if zone not in dict(TIMEZONES):
        zone = "UTC"
    try:
        exported = parse_export(read_export_bytes(upload.filename, data), timezone=zone, day_first=not form.get("month_first"))
    except ValueError as error:
        return _done(request, "/dashboard/knowledge", f"This file cannot be read: {error}.", "bad")
    if not exported:
        return _done(request, "/dashboard/knowledge", "No messages found in this file. Is it a WhatsApp chat export?", "bad")
    labels = _labels(request)
    chat = str(form.get("chat", ""))
    label = " ".join(str(form.get("label", "")).split())[:80]
    if chat:
        label = _chat_name(chat, labels)
    else:
        if not label:
            return _done(request, "/dashboard/knowledge", "Name the new conversation.", "bad")
        chat = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:60] or "conversation"
    state = _state(request)
    if not hasattr(state, "pending_imports"):
        state.pending_imports = {}
    now = time.monotonic()
    state.pending_imports = {k: v for k, v in state.pending_imports.items() if v["expires"] > now}
    token = secrets.token_urlsafe(12)
    state.pending_imports[token] = {
        "member": member,
        "filename": upload.filename[:120],
        "chat": chat,
        "label": label,
        "messages": exported,
        "count": len(exported),
        "people": len({m.author for m in exported}),
        "first": exported[0].sent_at,
        "last": exported[-1].sent_at,
        "expires": now + 1800,
    }
    return RedirectResponse(f"/dashboard/knowledge?preview={token}", status_code=303)


@router.post("/dashboard/knowledge/import")
async def knowledge_import(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    state = _state(request)
    pending = getattr(state, "pending_imports", {}).pop(str(form.get("token", "")), None)
    if not pending or pending["member"] != member:
        return _done(request, "/dashboard/knowledge", "This file expired: read it again.", "bad")
    store = _store(request)
    exported, chat = pending["messages"], pending["chat"]
    messages = [
        StoredMessage(id=id_, chat_id=chat, source="whatsapp_export", author=m.author, sent_at=m.sent_at, text=m.text)
        for id_, m in zip(message_ids(chat, exported), exported)
    ]
    added = await store.add_messages(messages)
    runtime = state.runtime
    if pending["label"] != _chat_name(chat, _labels(request)):
        await runtime.update({"chat_labels": {**runtime["chat_labels"], chat: pending["label"]}}, member, f"Named a conversation “{pending['label']}”")
    await store.add_audit(member, f"Added {added:,} old messages to “{pending['label']}”")
    memory = getattr(state, "activities", {}).get("memory")
    if memory and added:
        memory.run_now(member)
    known = len(messages) - added
    return _done(
        request,
        "/dashboard/knowledge",
        f"{added:,} new messages added{f' ({known:,} were already known)' if known else ''}. Jeli is learning them now: they can be asked about in a few minutes.",
    )


@router.post("/dashboard/knowledge/rename")
async def knowledge_rename(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    chat, label = str(form.get("chat", "")), " ".join(str(form.get("label", "")).split())[:80]
    if not chat or not label:
        return _done(request, "/dashboard/knowledge", "A conversation needs a name.", "bad")
    runtime = _state(request).runtime
    await runtime.update({"chat_labels": {**runtime["chat_labels"], chat: label}}, member, f"Renamed a conversation “{label}”")
    return _done(request, "/dashboard/knowledge", f"Jeli now cites it as “{label}”.")


# --- Activities -----------------------------------------------------------------------------------


@router.get("/dashboard/activities", response_class=HTMLResponse)
async def activities_page(request: Request, member: Member) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    state, csrf = _state(request), _csrf(request)
    runtime = state.runtime
    items = []
    for activity in getattr(state, "activities", {}).values():
        meta = []
        if activity.running:
            meta.append(f"<span>{icon('run', 14)} Running since {when(activity.last_started, 'time')}</span>")
        elif activity.last_finished:
            meta.append(f"<span>{icon('check', 14)} Last: {esc(activity.last_result)}, {when(activity.last_finished, 'ago')}</span>")
        if activity.enabled and activity.next_at and not activity.running:
            meta.append(f"<span>{icon('clock', 14)} Next: {when(activity.next_at)}</span>")
        if reason := activity.blocked():
            meta.append(f"<span>{icon('alert', 14)} {esc(reason)}</span>")
        schedule = ""
        if activity.key == "daily_summary":
            at = runtime["daily_digest_time"]
            language = runtime["daily_digest_language"]
            schedule = ui.form(
                "/dashboard/activities",
                csrf,
                ui.hidden("key", activity.key) + ui.hidden("action", "schedule")
                + f'<span class="small">Every day at</span><input type="time" name="at" value="{at}" required><span class="small">GMT, in</span>'
                + f'<select name="language"><option value="en"{" selected" if language == "en" else ""}>English</option><option value="fr"{" selected" if language == "fr" else ""}>French</option></select>'
                + ui.button("Save", kind="small")
                + f'<span class="muted small">{esc(_in_cities(at))}</span>',
                cls="schedule",
            )
        elif activity.key == "team_report":
            day, at = parse_schedule(runtime["team_report_time"])
            days = "".join(f'<option value="{WEEKDAYS[i]}"{" selected" if i == day else ""}>{WEEKDAY_NAMES[i]}</option>' for i in range(7))
            schedule = ui.form(
                "/dashboard/activities",
                csrf,
                ui.hidden("key", activity.key) + ui.hidden("action", "schedule")
                + f'<span class="small">Every</span><select name="day">{days}</select><span class="small">at</span>'
                + f'<input type="time" name="at" value="{at:%H:%M}" required><span class="small">GMT</span>'
                + ui.button("Save", kind="small")
                + f'<span class="muted small">{esc(_in_cities(f"{at:%H:%M}"))}</span>',
                cls="schedule",
            )
        buttons = (
            ui.form("/dashboard/activities", csrf, ui.hidden("key", activity.key) + ui.hidden("action", "stop") + ui.button("Stop", kind="small danger", icon_name="stop"), cls="inline", confirm=f"Stop “{activity.name}” now? It starts again at its next time.")
            if activity.running
            else ui.form("/dashboard/activities", csrf, ui.hidden("key", activity.key) + ui.hidden("action", "run") + ui.button(RUN_LABELS.get(activity.key, "Run now"), kind="small", icon_name="run"), cls="inline", confirm=RUN_CONFIRM.get(activity.key, ""))
        )
        toggle = ui.switch("/dashboard/activities", csrf, on=activity.enabled, name="action", label=f"{activity.name} on or off", fields=ui.hidden("key", activity.key))
        items.append(
            f'<div class="activity">{toggle}<div><h3>{esc(activity.name)}</h3><p>{esc(activity.description)}</p>'
            f'<div class="meta">{"".join(meta)}</div>{schedule}</div><div class="activity-side">{_activity_pill(activity)}{buttons}</div></div>'
        )
    body = ui.card(
        "Background activities",
        "".join(items) or ui.empty("No background activity: the knowledge base is not connected."),
        icon_name="activity",
        description="What Jeli does on its own. Switch an activity off, run it now, or stop it while it runs.",
    ) + ui.notice("info", "Times are in GMT (Bamako, Dakar); the equivalents in the team's cities are shown next to them.")
    return _page(request, member, title="Activities", subtitle="What Jeli does on its own, and when", active="activities", body=body, live=True)


RUN_LABELS = {"daily_summary": "Post now", "team_report": "Send now"}
RUN_CONFIRM = {
    "daily_summary": "Post today's summary in the groups now? Each group gets it once a day at most.",
    "team_report": "Send this week's report to the team now? Each member gets it once a week at most.",
}


@router.post("/dashboard/activities")
async def activities_change(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    state = _state(request)
    activity = getattr(state, "activities", {}).get(str(form.get("key", "")))
    if activity is None:
        return _done(request, "/dashboard/activities", "Unknown activity.", "bad")
    action = str(form.get("action", ""))
    runtime = state.runtime
    if action in ("on", "off"):
        await runtime.update({f"enabled.{activity.key}": action == "on"}, member, f"Switched {action} “{activity.name}”")
        return _done(request, "/dashboard/activities", f"“{activity.name}” is {action}.")
    if action == "run":
        if reason := activity.blocked():
            return _done(request, "/dashboard/activities", f"“{activity.name}” cannot run now: {reason}", "warn")
        if not activity.run_now(member):
            return _done(request, "/dashboard/activities", f"“{activity.name}” is already running.", "info")
        await _store(request).add_audit(member, f"Started “{activity.name}”")
        return _done(request, "/dashboard/activities", f"“{activity.name}” started.")
    if action == "stop":
        if activity.stop():
            await _store(request).add_audit(member, f"Stopped “{activity.name}”")
            return _done(request, "/dashboard/activities", f"“{activity.name}” stopped.", "warn")
        return _done(request, "/dashboard/activities", f"“{activity.name}” was not running.", "info")
    if action == "schedule":
        try:
            if activity.key == "daily_summary":
                await runtime.update(
                    {"daily_digest_time": form.get("at", ""), "daily_digest_language": form.get("language", "en")},
                    member,
                    f"Set the daily summary to {form.get('at')} GMT",
                )
            elif activity.key == "team_report":
                value = f"{form.get('day', '')} {form.get('at', '')}"
                await runtime.update({"team_report_time": value}, member, f"Set the weekly report to {value} GMT")
        except ValueError:
            return _done(request, "/dashboard/activities", "That time is not valid.", "bad")
        return _done(request, "/dashboard/activities", "Schedule saved.")
    return _done(request, "/dashboard/activities", "Unknown action.", "bad")


# --- Watchlist ------------------------------------------------------------------------------------


@router.get("/dashboard/watchlist", response_class=HTMLResponse)
async def watchlist_page(request: Request, member: Member) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    state, csrf = _state(request), _csrf(request)
    people = await _store(request).incidents_since(_now() - timedelta(days=WEEK))
    blocked = ignored_keys(state.runtime["muted_members"])
    guard = getattr(state, "guard", None)
    rows = []
    for person in people:
        key = person["member_key"]
        what = "<br>".join(f"{esc(INCIDENTS.get(kind, kind))} <span class='muted'>× {n}</span>" for kind, n in person["kinds"].items())
        quiet = guard.quiet_until(key) if guard else None
        if key in blocked:
            status = ui.pill("bad", "Blocked by the team")
        elif quiet:
            back = _now() + timedelta(seconds=quiet - time.monotonic())
            status = ui.pill("warn", "Silenced for now") + f'<br><span class="muted small">until {when(back, "time")}</span>'
        else:
            status = ui.pill("neutral", "Watching")
        block = (
            ui.form("/dashboard/watchlist", csrf, ui.hidden("ref", _ref(key)) + ui.hidden("action", "unblock") + ui.button("Unblock", kind="small"), cls="inline")
            if key in blocked
            else ui.form("/dashboard/watchlist", csrf, ui.hidden("ref", _ref(key)) + ui.hidden("action", "block") + ui.button("Block", kind="small danger", icon_name="ban"), cls="inline", confirm="Block this person? Jeli will never answer them again, until you unblock them.")
        )
        forgive = ui.form("/dashboard/watchlist", csrf, ui.hidden("ref", _ref(key)) + ui.hidden("action", "forgive") + ui.button("Forgive", kind="small ghost"), cls="inline")
        rows.append([_person(key, person["member_name"]), what, when(person["last_at"], "ago"), status, f'<div class="actions">{block}{forgive}</div>'])
    how = """<ul class="steps">
      <li><b>Too many questions:</b> past the limit per member (Settings → Pace), Jeli stops answering that person for a while.</li>
      <li><b>The same message again and again:</b> the third time in 10 minutes, Jeli stays silent.</li>
      <li><b>Very long messages:</b> ignored, they are used to overload the AI.</li>
      <li><b>Attempts to make Jeli ignore its rules:</b> noted. Jeli still answers only from the groups' messages.</li>
      <li><b>Three of these within an hour:</b> Jeli stays silent with that person for an hour, on its own.</li></ul>"""
    body = ui.card(
        "People to watch",
        ui.table(["Person", "What happened", "Last time", "Status", ""], rows, empty_text="Nobody has misused Jeli this week."),
        icon_name="shield",
        description="Members who tried to wear Jeli out or turn it against its rules, over the last 7 days.",
    ) + ui.card("How Jeli protects itself", how, icon_name="info")
    return _page(request, member, title="Watchlist", subtitle="Spot and stop misuse", active="watch", body=body, live=True)


@router.post("/dashboard/watchlist")
async def watchlist_change(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    ref, action = str(form.get("ref", "")), str(form.get("action", ""))
    state = _state(request)
    runtime = state.runtime
    people = {p["member_key"]: p["member_name"] for p in await _store(request).incidents_since(_now() - timedelta(days=WEEK))}
    for blocked in runtime["muted_members"]:
        people.setdefault(next(iter(ignored_keys([blocked]))), "")
    key = next((k for k in people if _ref(k) == ref), None)
    if key is None:
        return _done(request, "/dashboard/watchlist", "Unknown person.", "bad")
    who = people[key] or display_author(key)
    muted = runtime["muted_members"]
    if action == "block":
        await runtime.update({"muted_members": [*muted, key]}, member, f"Blocked {who}")
        return _done(request, "/dashboard/watchlist", f"Jeli won't answer {who} anymore.", "warn")
    if action == "unblock":
        await runtime.update({"muted_members": [m for m in muted if ignored_keys([m]) != {key}]}, member, f"Unblocked {who}")
        return _done(request, "/dashboard/watchlist", f"{who} is unblocked.")
    if action == "forgive":
        await _store(request).forgive(key)
        if getattr(state, "guard", None):
            state.guard.forgive(key)
        await _store(request).add_audit(member, f"Forgave {who}")
        return _done(request, "/dashboard/watchlist", f"{who} is off the watchlist.")
    return _done(request, "/dashboard/watchlist", "Unknown action.", "bad")


# --- Exceptions -----------------------------------------------------------------------------------

LISTS = {
    "ignored_authors": ("People Jeli never quotes", "Their messages are never used as sources — for example other bots in the group."),
    "muted_members": ("People Jeli doesn't answer", "Jeli stays silent when they call it. Add a name as shown on WhatsApp, or a phone number."),
}


@router.get("/dashboard/exceptions", response_class=HTMLResponse)
async def exceptions_page(request: Request, member: Member) -> HTMLResponse:
    state, csrf = _state(request), _csrf(request)
    runtime = state.runtime
    cards = []
    for key, (title, description) in LISTS.items():
        tags = "".join(
            f'<span class="tag">{esc(display_author(value))}'
            + ui.form("/dashboard/exceptions", csrf, ui.hidden("list", key) + ui.hidden("action", "remove") + ui.hidden("value", value) + f'<button class="btn ghost small" aria-label="Remove {esc(display_author(value))}">×</button>', cls="inline")
            + "</span>"
            for value in runtime[key]
        ) or '<span class="muted">Nobody yet.</span>'
        add = ui.form(
            "/dashboard/exceptions",
            csrf,
            ui.hidden("list", key) + ui.hidden("action", "add")
            + '<input name="value" required maxlength="80" placeholder="Name or phone number" style="max-width:280px">'
            + ui.button("Add", kind="small", icon_name="plus"),
            cls="actions",
        )
        cards.append(ui.card(title, f'<div class="tags">{tags}</div><div style="height:14px"></div>{add}', icon_name="ban", description=description))

    whatsapp = getattr(state, "whatsapp", None)
    names = await whatsapp.group_names() if whatsapp is not None and whatsapp.status == "WORKING" else {}
    store = getattr(state, "store", None)
    seen = await store.live_groups(_now() - timedelta(days=90)) if store else []
    chosen = runtime["groups"]
    labels = _labels(request)
    known = {g: names.get(g) or labels.get(g) or "A WhatsApp group" for g in [*names, *seen, *chosen]}
    boxes = "".join(
        f'<label class="check"><input type="checkbox" name="groups" value="{esc(g)}"{" checked" if g in chosen else ""}>{esc(name)}</label>'
        for g, name in sorted(known.items(), key=lambda item: item[1].lower())
    ) or '<p class="muted">Jeli\'s groups appear here once its WhatsApp is connected.</p>'
    groups = ui.form(
        "/dashboard/groups",
        csrf,
        f"""<div class="fields">
          <label class="check"><input type="radio" name="mode" value="all"{" checked" if not chosen else ""}> Every group Jeli is in</label>
          <label class="check"><input type="radio" name="mode" value="selected"{" checked" if chosen else ""}> Only the groups ticked below</label>
          <div class="fields" style="padding-left:28px">{boxes}</div>
        </div><div class="actions" style="margin-top:14px">{ui.button("Save", kind="primary")}</div>""",
    )
    body = "".join(cards) + ui.card("Groups Jeli works in", groups, icon_name="users", description="In the other groups, Jeli reads nothing and answers nobody. Private messages always reach it.")
    return _page(request, member, title="Exceptions", subtitle="Who Jeli ignores, and where it works", active="exceptions", body=body)


@router.post("/dashboard/exceptions")
async def exceptions_change(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    key, action, value = str(form.get("list", "")), str(form.get("action", "")), " ".join(str(form.get("value", "")).split())[:80]
    if key not in LISTS or not value:
        return _done(request, "/dashboard/exceptions", "Type a name or a number.", "bad")
    runtime = _state(request).runtime
    current = runtime[key]
    title = LISTS[key][0].lower()
    if action == "add":
        await runtime.update({key: [*current, value]}, member, f"Added {display_author(value)} to {title}")
        return _done(request, "/dashboard/exceptions", f"Added {display_author(value)}.")
    await runtime.update({key: [v for v in current if v != value]}, member, f"Removed {display_author(value)} from {title}")
    return _done(request, "/dashboard/exceptions", f"Removed {display_author(value)}.")


@router.post("/dashboard/groups")
async def groups_change(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    chosen = [str(g) for g in form.getlist("groups") if str(g).endswith("@g.us")] if form.get("mode") == "selected" else []
    if form.get("mode") == "selected" and not chosen:
        return _done(request, "/dashboard/exceptions", "Tick at least one group, or choose every group.", "bad")
    await _state(request).runtime.update({"groups": chosen}, member, f"Set Jeli to work in {len(chosen)} group(s)" if chosen else "Set Jeli to work in every group")
    return _done(request, "/dashboard/exceptions", "Groups saved.")


# --- Settings -------------------------------------------------------------------------------------


def _nearest(value: float, choices: dict[str, float]) -> str:
    return min(choices, key=lambda name: abs(choices[name] - value))


def _segmented(name: str, current: str, options: list[tuple[str, str]]) -> str:
    return '<div class="segmented" role="radiogroup">' + "".join(
        f'<label><input type="radio" name="{name}" value="{value}"{" checked" if value == current else ""}><span>{esc(label)}</span></label>'
        for value, label in options
    ) + "</div>"


def _row(title: str, text: str, control: str) -> str:
    return f'<div class="row"><div class="row-text"><b>{esc(title)}</b><span>{esc(text)}</span></div><div class="row-side">{control}</div></div>'


@router.get("/dashboard/settings", response_class=HTMLResponse)
async def settings_page(request: Request, member: Member) -> HTMLResponse:
    runtime, csrf = _state(request).runtime, _csrf(request)
    care = _nearest(runtime["answer_min_similarity"], CARE)
    pointer_care = _nearest(runtime["duplicate_min_similarity"], POINTER_CARE)

    def number(name: str, low: int, high: int, step: str = "1") -> str:
        return f'<input type="number" name="{name}" value="{runtime[name]:g}" min="{low}" max="{high}" step="{step}" style="width:96px">'

    answers = (
        _row("How sure must Jeli be before answering?", "Careful: it says “I don't know” more often. Relaxed: it answers more, with a higher risk of a weak answer.",
             _segmented("care", care, [("careful", "Careful"), ("balanced", "Balanced"), ("relaxed", "Relaxed")]))
        + _row("Name members can call it by", "A message starting with this name is for Jeli, like an @mention.",
               f'<input name="bot_name" value="{esc(runtime["bot_name"])}" maxlength="60" style="width:180px">')
        + _row("Follow the conversation", "After answering someone, Jeli takes their next questions for a few minutes without them repeating its name.",
               f'<label class="check"><input type="checkbox" name="follow_up"{" checked" if runtime["follow_up"] else ""}> On</label>'
               + number("follow_up_minutes", 1, 15) + '<span class="muted small">minutes</span>')
        + _row("Add recorded sessions shared in the groups", "When someone shares a session's YouTube recording in a group, Jeli transcribes it and learns it.",
               f'<label class="check"><input type="checkbox" name="auto_sessions"{" checked" if runtime["auto_sessions"] else ""}> On</label>')
    )
    pointers = (
        _row("Point to earlier answers", "When someone asks the group a question it already answered, Jeli replies with a link to that answer, without being called.",
             f'<label class="check"><input type="checkbox" name="duplicate_detection"{" checked" if runtime["duplicate_detection"] else ""}> On</label>')
        + _row("How similar must the question be?", "Careful: only near-identical questions.", _segmented("pointer_care", pointer_care, [("careful", "Careful"), ("balanced", "Balanced")]))
        + _row("At most, per group and per hour", "Jeli speaks uninvited rarely, to stay discreet.", number("duplicate_replies_per_hour", 1, 20) + '<span class="muted small">times</span>')
    )
    pace = (
        ui.notice("warn", "WhatsApp blocks numbers that behave like machines. Raise these only if members really need it.")
        + '<div style="height:8px"></div>'
        + _row("Answers per member", "In any 10 minutes. Beyond it, Jeli waits — and notes the person on the watchlist.", number("whatsapp_user_limit", 1, 20))
        + _row("Answers per hour, all groups together", "A safety net: Jeli stops for the rest of the hour.", number("whatsapp_hourly_limit", 5, 300))
        + _row("Pause between two messages", "At least this many seconds between two messages Jeli sends.", number("whatsapp_min_send_interval_seconds", 1, 60, "0.5") + '<span class="muted small">seconds</span>')
    )
    body = ui.form(
        "/dashboard/settings",
        csrf,
        ui.card("Answers", f'<div class="rows">{answers}</div>', icon_name="chat")
        + '<div style="height:20px"></div>'
        + ui.card("Earlier answers", f'<div class="rows">{pointers}</div>', icon_name="sparkle")
        + '<div style="height:20px"></div>'
        + ui.card("Pace", f'<div class="rows">{pace}</div>', icon_name="shield", description="Protects Jeli's WhatsApp number.")
        + f'<div class="actions" style="margin-top:20px">{ui.button("Save the settings", kind="primary", icon_name="check")}</div>',
    )
    return _page(request, member, title="Settings", subtitle="How Jeli answers and how fast", active="settings", body=body)


SETTING_WORDS = {
    "answer_min_similarity": "how sure Jeli must be",
    "bot_name": "Jeli's name",
    "follow_up": "following the conversation",
    "follow_up_minutes": "how long Jeli follows a conversation",
    "auto_sessions": "adding shared recordings",
    "duplicate_detection": "pointing to earlier answers",
    "duplicate_min_similarity": "how similar a repeated question must be",
    "duplicate_replies_per_hour": "earlier answers per hour",
    "whatsapp_user_limit": "answers per member",
    "whatsapp_hourly_limit": "answers per hour",
    "whatsapp_min_send_interval_seconds": "pause between messages",
}


@router.post("/dashboard/settings")
async def settings_change(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    changes = {
        "answer_min_similarity": CARE.get(str(form.get("care")), 0.60),
        "bot_name": form.get("bot_name", ""),
        "follow_up": bool(form.get("follow_up")),
        "follow_up_minutes": form.get("follow_up_minutes", "5"),
        "auto_sessions": bool(form.get("auto_sessions")),
        "duplicate_detection": bool(form.get("duplicate_detection")),
        "duplicate_min_similarity": POINTER_CARE.get(str(form.get("pointer_care")), 0.70),
        "duplicate_replies_per_hour": form.get("duplicate_replies_per_hour", ""),
        "whatsapp_user_limit": form.get("whatsapp_user_limit", ""),
        "whatsapp_hourly_limit": form.get("whatsapp_hourly_limit", ""),
        "whatsapp_min_send_interval_seconds": form.get("whatsapp_min_send_interval_seconds", ""),
    }
    runtime = _state(request).runtime
    for key, value in changes.items():
        try:
            changes[key] = coerce(FIELDS[key], value)
        except ValueError as error:
            return _done(request, "/dashboard/settings", f"Check “{SETTING_WORDS[key]}”: it {error}.", "bad")
    changed = sorted(key for key, value in changes.items() if value != runtime[key])
    if not changed:
        return _done(request, "/dashboard/settings", "Nothing changed.", "info")
    await runtime.update(changes, member, "Changed " + ", ".join(SETTING_WORDS[key] for key in changed))
    return _done(request, "/dashboard/settings", "Settings saved.")


# --- WhatsApp -------------------------------------------------------------------------------------


@router.get("/dashboard/whatsapp", response_class=HTMLResponse)
async def whatsapp_page(request: Request, member: Member) -> HTMLResponse:
    state, csrf = _state(request), _csrf(request)
    whatsapp = getattr(state, "whatsapp", None)
    level, headline, explanation = whatsapp_state(state)
    if whatsapp is not None and whatsapp.status != "WORKING":
        await whatsapp.sync_status()
        level, headline, explanation = whatsapp_state(state)
    status = f'<div class="actions">{ui.pill(level, headline)}</div><p style="margin:12px 0 0">{esc(explanation)}</p>'
    extra = ""
    if whatsapp is not None and whatsapp.status == "WORKING":
        me = await whatsapp.me() or {}
        number = display_author("+" + me["id"].split("@")[0]) if me.get("id") else "—"
        extra = f'<div class="hero" style="padding:16px 0 0"><img src="/dashboard/avatar" class="avatar" alt=""><div class="hero-text"><h2>{esc(me.get("pushName") or "Jeli")}</h2><p>{esc(number)}</p></div></div>'
    elif whatsapp is not None and whatsapp.status == "SCAN_QR_CODE":
        extra = """<div class="grid two" style="margin-top:16px;align-items:center">
          <div class="qr"><img id="qr" src="/dashboard/whatsapp/qr.png" alt="Code to scan with Jeli's phone"></div>
          <ol class="steps"><li>Take Jeli's phone.</li><li>Open WhatsApp → <b>Settings</b> → <b>Linked devices</b>.</li>
          <li>Tap <b>Link a device</b> and scan this code.</li><li>This page updates by itself once connected.</li></ol></div>
          <script>setInterval(() => { const q = document.getElementById('qr'); q.src = '/dashboard/whatsapp/qr.png?' + Date.now(); }, 20000);
          setInterval(() => location.reload(), 60000);</script>"""
    elif whatsapp is not None:
        extra = '<div class="actions" style="margin-top:16px">' + ui.form(
            "/dashboard/whatsapp/restart",
            csrf,
            ui.button("Connect Jeli's phone", kind="primary", icon_name="phone"),
            confirm="Start the connection? A code to scan will appear. Only do it once the number's warm-up is over.",
            cls="inline",
        ) + "</div>"
    safety = """<ul class="steps">
      <li><b>Warm the number up first:</b> use Jeli's SIM as a normal phone for a few days before linking it.</li>
      <li><b>If WhatsApp restricts the number, don't reconnect:</b> the restriction lifts on its own; reconnecting makes it worse.</li>
      <li><b>Keep the pace settings low:</b> Jeli answers only when called, like a person, never in bursts.</li>
      <li><b>Team members:</b> save Jeli's number and write to it once, so its weekly report arrives in a conversation you started.</li></ul>"""
    body = ui.card("Connection", status + extra, icon_name="phone", description="Jeli lives in WhatsApp through its own phone number.") + ui.card(
        "Keeping the number safe", safety, icon_name="shield"
    )
    return _page(request, member, title="WhatsApp", subtitle="Jeli's phone number and its connection", active="whatsapp", body=body)


@router.get("/dashboard/whatsapp/qr.png")
async def whatsapp_qr(request: Request, member: Member) -> Response:
    whatsapp = getattr(_state(request), "whatsapp", None)
    png = await whatsapp.qr_code() if whatsapp is not None else None
    if not png:
        raise HTTPException(status_code=404)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/dashboard/whatsapp/restart")
async def whatsapp_restart(request: Request, member: Change) -> RedirectResponse:
    whatsapp = getattr(_state(request), "whatsapp", None)
    if whatsapp is None:
        return _done(request, "/dashboard/whatsapp", "WhatsApp is not set up on the server.", "bad")
    if whatsapp.status == "WORKING":
        return _done(request, "/dashboard/whatsapp", "Jeli is already connected.", "info")
    ok = await whatsapp.restart_session()
    await _store(request).add_audit(member, "Started the WhatsApp connection")
    return _done(request, "/dashboard/whatsapp", "Connecting: the code to scan appears in a few seconds." if ok else "The connection could not start. Try again in a minute.", "good" if ok else "bad")


# --- Team -----------------------------------------------------------------------------------------


@router.get("/dashboard/team", response_class=HTMLResponse)
async def team_page(request: Request, member: Member) -> Response:
    if (unchanged := await _unchanged(request)) is not None:
        return unchanged
    state, csrf = _state(request), _csrf(request)
    auth = auth_of(request)
    members = "".join(
        f'<div class="row"><div class="row-text"><b>{esc(name.capitalize())}</b><span>Can use this dashboard</span></div>'
        f'<div class="row-side">{ui.pill("info", "You") if name == member else ""}</div></div>'
        for name in sorted(auth.accounts)
    )
    log_rows = [[when(entry["at"]), f'<span class="strong">{esc(entry["actor"].capitalize())}</span>', esc(entry["action"])] for entry in await _store(request).audit_log(100)]
    password = ui.form(
        "/dashboard/team/password",
        csrf,
        """<div class="fields">
          <label>Current password<input type="password" name="current" autocomplete="current-password" required></label>
          <label>New password<input type="password" name="new" autocomplete="new-password" minlength="10" required></label>
          <label>New password again<input type="password" name="again" autocomplete="new-password" minlength="10" required></label>
        </div><div class="actions" style="margin-top:14px">"""
        + ui.button("Change my password", kind="primary", icon_name="key")
        + "</div>",
    )
    report = len(get_settings().team_number_list)
    members_card = ui.card(
        "Members",
        f'<div class="rows">{members}</div>',
        icon_name="users",
        description=f"The weekly report goes to {report} team member{'s' if report != 1 else ''} on WhatsApp.",
    )
    password_card = ui.card("My password", password, icon_name="key", description="At least 10 characters. Nobody else sees it, not even the team.")
    log_card = ui.card("Activity log", ui.table(["When", "Who", "What"], log_rows, empty_text="Nothing yet."), icon_name="list", description="Every change made on this dashboard, and by whom.")
    body = f'<div class="grid two">{members_card}{password_card}</div>{log_card}'
    return _page(request, member, title="Team", subtitle="Who runs Jeli, and who did what", active="team", body=body, live=True)


@router.post("/dashboard/team/password")
async def team_password(request: Request, member: Change) -> RedirectResponse:
    form = await request.form()
    new = str(form.get("new", ""))
    if new != str(form.get("again", "")):
        return _done(request, "/dashboard/team", "The two new passwords are not the same.", "bad")
    problem = await auth_of(request).change_password(member, str(form.get("current", "")), new)
    if problem:
        return _done(request, "/dashboard/team", problem, "bad")
    return _done(request, "/dashboard/team", "Your password is changed.")

