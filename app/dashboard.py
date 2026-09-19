"""Web dashboard (R13), for the team: what Jeli knows, how it is used, and what the groups ask it —
never who asked.

GET /dashboard with HTTP Basic auth: one account per team member (DASHBOARD_USERS), each with its
own password, stored as a scrypt hash. Without accounts, the page does not exist. It refreshes
itself every minute.
"""

import asyncio
import hashlib
import html
import logging
import math
import secrets
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import get_settings
from app.ingest.transcribe import format_offset

log = logging.getLogger(__name__)
router = APIRouter()
basic = HTTPBasic(auto_error=False)
SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}

DAYS = 7
# Question outcomes, in the fixed categorical order of the chart (series 1, 2, 3).
OUTCOMES = [("answered", "Answered"), ("dont_know", "“I don't know”"), ("sources_only", "Sources only (models down)")]
# State is never carried by colour alone: each chip also has an icon and a word.
ICONS = {"good": "✓", "warning": "!", "neutral": "–"}
KINDS = [("catchup", "Catch-ups"), ("recap", "Session recaps"), ("deadlines", "Deadline lists"), ("search", "Searches"), ("already_answered", "“Already answered” pointers")]


def hash_password(password: str, salt: bytes | None = None) -> str:
    """"salt:hash" in hex, the form an account keeps in DASHBOARD_USERS."""
    salt = salt or secrets.token_bytes(16)
    return f"{salt.hex()}:{hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        salt = bytes.fromhex(stored.split(":", 1)[0])
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored)


# Checked against when the name is unknown, so a wrong name takes as long as a wrong password.
_NOBODY = f"{'00' * 16}:{'00' * 32}"


async def _authorise(credentials: HTTPBasicCredentials | None) -> str:
    """The signed-in account name, or 404 without accounts / 401 without valid credentials."""
    accounts = get_settings().dashboard_accounts
    if not accounts:
        raise HTTPException(status_code=404)
    if credentials is not None:
        name = credentials.username.strip().lower()
        stored = accounts.get(name)
        if await asyncio.to_thread(check_password, credentials.password, stored or _NOBODY) and stored:
            return name
        log.warning("Dashboard: refused sign-in as %r", credentials.username[:40])
    raise HTTPException(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Jeli dashboard"'})


@router.get("/", include_in_schema=False)
async def home() -> RedirectResponse:
    return RedirectResponse("/dashboard")


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard(request: Request, credentials: Annotated[HTTPBasicCredentials | None, Depends(basic)]) -> HTMLResponse:
    viewer = await _authorise(credentials)
    state = request.app.state
    now = datetime.now(timezone.utc)
    store = getattr(state, "store", None)
    first_day = datetime.combine((now - timedelta(days=DAYS - 1)).date(), time.min, timezone.utc)
    usage = await store.usage_since(first_day) if store else None
    knowledge = await store.knowledge_overview() if store else None
    page = render(now, status(state), usage, knowledge, get_settings().chat_label_map, viewer)
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


def status(state) -> list[tuple[str, str, str]]:
    """(component, level, detail); level is good, warning or neutral."""
    rows = []
    whatsapp = getattr(state, "whatsapp", None)
    if whatsapp is None:
        rows.append(("WhatsApp", "neutral", "not configured"))
    elif whatsapp.status == "WORKING":
        rows.append(("WhatsApp", "good", "connected"))
    else:
        detail = "number not linked yet" if whatsapp.status in (None, "FAILED", "SCAN_QR_CODE", "STARTING") else whatsapp.status
        rows.append(("WhatsApp", "warning", f"{detail} ({whatsapp.status or 'unknown'})"))
    rows.append(("Answers", "good", "ready") if getattr(state, "answerer", None) else ("Answers", "warning", "no knowledge base"))
    for name, attribute, running in (
        ("Indexing", "indexing", "every 5 minutes"),
        ("Deadline scan", "deadline_scan", "every hour"),
    ):
        task = getattr(state, attribute, None)
        alive = task is not None and not task.done()
        rows.append((name, "good", running) if alive else (name, "warning" if task else "neutral", "stopped" if task else "off"))
    digest_time = get_settings().daily_digest_time
    rows.append(("Daily digest", "good", f"at {digest_time} UTC") if getattr(state, "daily_digest", None) else ("Daily digest", "neutral", "off"))
    llm = getattr(state, "llm", None)
    for model, resting in llm.status() if llm else []:
        rows.append((model, "good", "available") if not resting else (model, "warning", f"resting {resting} s (quota or overload)"))
    return rows


def _nice_ceiling(value: int) -> tuple[int, int]:
    """A round axis maximum and its step, with at most 5 intervals: 0 / 5 / 10 / 15 …"""
    if value <= 4:
        return 4, 1
    magnitude = 10 ** max(0, math.floor(math.log10(value)) - 1)
    for step in (m * magnitude for m in (1, 2, 5, 10, 20, 25, 50, 100)):
        if math.ceil(value / step) <= 5:
            return math.ceil(value / step) * step, step
    return value, value


def _chart(days: list[date], counts: dict[tuple[date, str], int]) -> str:
    width, height, left, right, top, bottom = 560, 220, 34, 8, 22, 28
    plot_h = height - top - bottom
    band = (width - left - right) / len(days)
    bar = min(24.0, band * 0.55)
    totals = {d: sum(counts.get((d, o), 0) for o, _ in OUTCOMES) for d in days}
    y_max, step = _nice_ceiling(max(totals.values(), default=0))
    baseline = top + plot_h
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-labelledby="chart-title">']
    for tick in range(0, y_max + 1, step):
        y = baseline - tick / y_max * plot_h
        parts.append(f'<line class="grid" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{tick}</text>')
    for i, day in enumerate(days):
        x = left + band * i + (band - bar) / 2
        detail = ", ".join(f"{counts.get((day, o), 0)} {label.lower()}" for o, label in OUTCOMES)
        parts.append(f'<g class="col"><title>{day:%a %d %b}: {totals[day]} questions — {html.escape(detail)}</title>')
        parts.append(f'<rect class="hit" x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}"/>')
        segments = [(o, counts.get((day, o), 0)) for o, _ in OUTCOMES if counts.get((day, o), 0)]
        cursor = baseline
        for n, (outcome, value) in enumerate(segments):
            seg_top = cursor - value / y_max * plot_h
            seg_bottom = cursor - (2 if n else 0)  # 2px surface gap between stacked segments
            h = seg_bottom - seg_top
            if h > 0.5:
                series = [o for o, _ in OUTCOMES].index(outcome) + 1
                if n == len(segments) - 1:  # 4px rounded data end, square at the baseline
                    r = min(4.0, h, bar / 2)
                    parts.append(
                        f'<path class="s{series}" d="M{x:.1f},{seg_bottom:.1f} L{x:.1f},{seg_top + r:.1f} '
                        f'Q{x:.1f},{seg_top:.1f} {x + r:.1f},{seg_top:.1f} L{x + bar - r:.1f},{seg_top:.1f} '
                        f'Q{x + bar:.1f},{seg_top:.1f} {x + bar:.1f},{seg_top + r:.1f} L{x + bar:.1f},{seg_bottom:.1f} Z"/>'
                    )
                else:
                    parts.append(f'<rect class="s{series}" x="{x:.1f}" y="{seg_top:.1f}" width="{bar:.1f}" height="{h:.1f}"/>')
            cursor = seg_top
        if totals[day]:
            parts.append(f'<text class="cap" x="{x + bar / 2:.1f}" y="{cursor - 6:.1f}" text-anchor="middle">{totals[day]}</text>')
        parts.append(f'<text class="tick" x="{x + bar / 2:.1f}" y="{height - 8}" text-anchor="middle">{day:%a %d}</text></g>')
    parts.append(f'<line class="axis" x1="{left}" x2="{width - right}" y1="{baseline}" y2="{baseline}"/>')
    if not any(totals.values()):
        parts.append(f'<text class="empty" x="{(left + width - right) / 2}" y="{top + plot_h / 2}" text-anchor="middle">No questions yet</text>')
    parts.append("</svg>")
    return "".join(parts)


def _pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "—"


SHOWN_QUESTIONS = 20
OUTCOME_WORDS = {"answered": "answered", "dont_know": "“I don't know”", "sources_only": "sources only", "not_ready": "not ready"}


def _questions(questions: list[tuple[datetime, str, str]]) -> str:
    """What members asked Jeli in the groups, newest first: the ones it could not answer (gaps to
    fill, in the group or with a source), then all of them (what the community needs)."""
    esc = html.escape

    def rows(items, with_outcome):
        return "".join(
            f"<tr><td>{at:%a %d %b %H:%M}</td><td>{esc(q)}</td>"
            + (f"<td>{esc(OUTCOME_WORDS.get(o, o))}</td>" if with_outcome else "")
            + "</tr>"
            for at, o, q in items[:SHOWN_QUESTIONS]
        )

    unanswered = [q for q in questions if q[1] == "dont_know"]
    return f"""
        <div class="panel"><h3>Jeli could not answer ({len(unanswered)})</h3>
          <p class="muted">Knowledge gaps: answer them in the group, or add the recording or document that does.</p>
          <div class="table-wrap"><table><thead><tr><th>Asked (UTC)</th><th>Question</th></tr></thead>
          <tbody>{rows(unanswered, False) or '<tr><td colspan="2" class="muted">None in the last 7 days</td></tr>'}</tbody></table></div></div>
        <div class="panel"><h3>All questions ({len(questions)})</h3>
          <p class="muted">What the community asks: material for the pitch, the FAQ and the next features.</p>
          <div class="table-wrap"><table><thead><tr><th>Asked (UTC)</th><th>Question</th><th>Outcome</th></tr></thead>
          <tbody>{rows(questions, True) or '<tr><td colspan="3" class="muted">No questions yet</td></tr>'}</tbody></table></div></div>"""


def render(now: datetime, rows: list[tuple[str, str, str]], usage: dict | None, knowledge: dict | None, labels: dict[str, str], viewer: str = "") -> str:
    esc = html.escape
    chips = "".join(
        f'<li class="chip {level}"><span class="dot" aria-hidden="true">{ICONS[level]}</span>'
        f"<b>{esc(name)}</b> {esc(detail)}</li>"
        for name, level, detail in rows
    )

    if usage is None:
        usage_html = '<p class="muted">No knowledge base configured: nothing to count.</p>'
    else:
        days = [(now - timedelta(days=DAYS - 1 - i)).date() for i in range(DAYS)]
        counts = {(day, outcome): n for day, outcome, n in usage["questions_by_day"]}
        by_outcome = {o: sum(n for (d, oc), n in counts.items() if oc == o) for o, _ in OUTCOMES}
        questions = usage["by_kind"].get("question", 0)
        median = f"{usage['median_ms'] / 1000:.1f} s" if usage["median_ms"] is not None else "—"
        p95 = f"95% under {usage['p95_ms'] / 1000:.1f} s" if usage["p95_ms"] is not None else "no questions yet"
        tiles = [
            ("Questions", f"{questions:,}", "last 7 days"),
            ("Answered with sources", _pct(by_outcome["answered"], questions), f"{by_outcome['answered']:,} questions"),
            ("“I don't know”", _pct(by_outcome["dont_know"], questions), f"{by_outcome['dont_know']:,} questions"),
            ("Median reply time", median, p95),
        ]
        tiles_html = "".join(f'<div class="tile"><span class="label">{esc(l)}</span><span class="value">{esc(v)}</span><span class="sub">{esc(s)}</span></div>' for l, v, s in tiles)
        others = "".join(f"<li><span>{esc(label)}</span><b>{usage['by_kind'].get(kind, 0):,}</b></li>" for kind, label in KINDS)
        legend = "".join(f'<li><span class="swatch s{i}" aria-hidden="true"></span>{esc(label)}</li>' for i, (_, label) in enumerate(OUTCOMES, 1))
        table_rows = "".join(
            f"<tr><td>{d:%a %d %b}</td>" + "".join(f'<td class="num">{counts.get((d, o), 0)}</td>' for o, _ in OUTCOMES)
            + f'<td class="num">{sum(counts.get((d, o), 0) for o, _ in OUTCOMES)}</td></tr>'
            for d in days
        )
        table_head = "".join(f'<th class="num">{esc(label)}</th>' for _, label in OUTCOMES)
        usage_html = f"""
        <div class="tiles">{tiles_html}</div>
        <div class="panel">
          <h3 id="chart-title">Questions per day, by outcome</h3>
          <ul class="legend">{legend}</ul>
          <div class="chart-wrap">{_chart(days, counts)}</div>
          <details><summary>Show as a table</summary>
            <div class="table-wrap"><table><thead><tr><th>Day (UTC)</th>{table_head}<th class="num">Total</th></tr></thead><tbody>{table_rows}</tbody></table></div>
          </details>
        </div>
        <ul class="counters">{others}</ul>"""

    questions_html = _questions(usage["group_questions"]) if usage else '<p class="muted">No knowledge base configured.</p>'

    if knowledge is None:
        knowledge_html = '<p class="muted">No knowledge base configured.</p>'
    else:
        chat_rows = "".join(
            f"<tr><td>{esc(labels.get(c['chat_id'], c['chat_id']))}</td><td class=\"num\">{c['messages']:,}</td>"
            f"<td class=\"num\">{c['live']:,}</td><td>{c['last_message']:%d %b %Y %H:%M}</td></tr>"
            for c in knowledge["chats"]
        )
        recording_rows = "".join(
            f"<tr><td>{esc(r['title'])}</td><td>{r['recorded_at']:%d %b %Y}</td>"
            f"<td class=\"num\">{format_offset(timedelta(seconds=r['duration_seconds'] or 0))}</td>"
            f"<td class=\"num\">{r['segments']:,}</td><td>{esc(', '.join(k.upper() for k in r['recaps']) or '—')}</td></tr>"
            for r in knowledge["recordings"]
        )
        knowledge_html = f"""
        <ul class="counters">
          <li><span>Conversation chunks indexed</span><b>{knowledge['chunks']:,}</b></li>
          <li><span>Messages waiting to be indexed</span><b>{knowledge['pending']:,}</b></li>
          <li><span>Upcoming deadlines found</span><b>{knowledge['deadlines']:,}</b></li>
        </ul>
        <div class="panel"><h3>Chats</h3><div class="table-wrap"><table>
          <thead><tr><th>Chat</th><th class="num">Messages</th><th class="num">Received live</th><th>Last message (UTC)</th></tr></thead>
          <tbody>{chat_rows or '<tr><td colspan="4" class="muted">No messages yet</td></tr>'}</tbody></table></div></div>
        <div class="panel"><h3>Call recordings</h3><div class="table-wrap"><table>
          <thead><tr><th>Session</th><th>Date</th><th class="num">Length</th><th class="num">Transcript segments</th><th>Recaps</th></tr></thead>
          <tbody>{recording_rows or '<tr><td colspan="5" class="muted">No recordings yet</td></tr>'}</tbody></table></div></div>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><meta name="robots" content="noindex">
<title>Jeli dashboard</title>
<style>{CSS}</style></head>
<body><main>
  <header>
    <h1>Jeli dashboard</h1>
    <p class="muted">Updated {now:%a %d %b %Y, %H:%M} UTC · refreshes every minute · no member is ever shown: questions asked in groups appear without their author, private questions never.{f" Signed in as {esc(viewer)}." if viewer else ""}</p>
  </header>
  <section><h2>Status</h2><ul class="chips">{chips}</ul></section>
  <section><h2>Usage</h2>{usage_html}</section>
  <section><h2>Questions from the groups</h2>{questions_html}</section>
  <section><h2>Knowledge</h2>{knowledge_html}</section>
</main></body></html>"""


CSS = """
:root {
  color-scheme: light;
  --page: #f6f6f4; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a;
  --good: #006300; --good-bg: #e4f3e4; --warning: #8a5a00; --warning-bg: #fdf1d8; --neutral-bg: #ecebe7;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
    --good: #0ca30c; --good-bg: #13301a; --warning: #fab219; --warning-bg: #3a2e12; --neutral-bg: #2a2a28;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70;
  --good: #0ca30c; --good-bg: #13301a; --warning: #fab219; --warning-bg: #3a2e12; --neutral-bg: #2a2a28;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; padding-inline: 16px; padding-block: 24px 48px; }
main { max-width: 1040px; margin: 0 auto; display: grid; gap: 28px; }
h1 { font-size: 24px; margin: 0; } h2 { font-size: 17px; margin: 0 0 12px; } h3 { font-size: 14px; margin: 0 0 10px; }
p { margin: 4px 0 0; }
.muted { color: var(--ink-2); }
.chips { list-style: none; padding: 0; margin: 0; display: flex; flex-wrap: wrap; gap: 8px; }
.chip { display: inline-flex; gap: 6px; align-items: center; padding: 6px 10px; border-radius: 999px; background: var(--neutral-bg); color: var(--ink); }
.chip.good { background: var(--good-bg); } .chip.good .dot { color: var(--good); }
.chip.warning { background: var(--warning-bg); } .chip.warning .dot { color: var(--warning); }
.chip .dot { font-weight: 700; width: 1em; text-align: center; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; margin-bottom: 12px; }
.tile, .panel { background: var(--surface); border-radius: 12px; box-shadow: 0 0 0 1px var(--ring); padding: 14px 16px; }
.tile { display: grid; gap: 2px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 28px; font-weight: 600; }
.tile .sub { color: var(--muted); font-size: 12px; }
.panel { margin-bottom: 12px; }
.legend { list-style: none; padding: 0; margin: 0 0 8px; display: flex; flex-wrap: wrap; gap: 6px 16px; color: var(--ink-2); font-size: 13px; }
.legend li { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.s1, .swatch.s1 { fill: var(--series-1); background: var(--series-1); }
.s2, .swatch.s2 { fill: var(--series-2); background: var(--series-2); }
.s3, .swatch.s3 { fill: var(--series-3); background: var(--series-3); }
.chart-wrap { overflow-x: auto; }
.chart { width: 100%; min-width: 420px; max-width: 760px; height: auto; display: block; }
.chart .grid { stroke: var(--grid); stroke-width: 1; }
.chart .axis { stroke: var(--axis); stroke-width: 1; }
.chart .tick { fill: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.chart .cap { fill: var(--ink-2); font-size: 11px; font-weight: 600; }
.chart .empty { fill: var(--muted); font-size: 13px; }
.chart .hit { fill: transparent; }
.chart:hover .col { opacity: 0.45; } .chart .col:hover { opacity: 1; }
@media (prefers-reduced-motion: no-preference) { .chart .col { transition: opacity 120ms; } }
details { margin-top: 10px; } summary { cursor: pointer; color: var(--ink-2); }
summary:focus-visible { outline: 2px solid var(--series-1); outline-offset: 2px; }
.counters { list-style: none; padding: 0; margin: 0 0 12px; display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }
.counters li { display: flex; justify-content: space-between; gap: 12px; background: var(--surface); border-radius: 10px; box-shadow: 0 0 0 1px var(--ring); padding: 10px 14px; }
.counters b { font-variant-numeric: tabular-nums; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-top: 1px solid var(--grid); white-space: nowrap; }
thead th { border-top: 0; color: var(--muted); font-weight: 500; }
td:first-child { white-space: normal; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
"""
