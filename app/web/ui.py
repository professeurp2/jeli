"""The dashboard's look: layout, components, icons and styles. Everything shown is escaped here.

Colours follow Jeli's lemur: charcoal and warm white, with the amber of its eyes as the accent.
Status is never shown by colour alone: every state also has an icon and a word.
"""

import html
from datetime import datetime, timedelta, timezone

esc = html.escape

# Line icons (24×24, stroke), drawn for the dashboard.
ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5.5 9v11.5h13V9"/><path d="M10 20.5v-6h4v6"/>',
    "chat": '<path d="M20.5 12a8.5 8.5 0 0 1-12.3 7.6L3.5 20.5l1-4.4A8.5 8.5 0 1 1 20.5 12Z"/>',
    "question": '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.4 2.4c-.6.3-1 .8-1 1.5v.5"/><path d="M12 16.8h.01"/>',
    "calendar": '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M8 3v4M16 3v4M3.5 10h17"/>',
    "book": '<path d="M4.5 18.5V5.5a2 2 0 0 1 2-2h13v14h-13a2 2 0 0 0-2 2 2 2 0 0 0 2 2h13"/><path d="M8.5 7.5h7"/>',
    "activity": '<path d="M3 12h4l2.5-7 5 14 2.5-7h4"/>',
    "shield": '<path d="M12 3 4.5 6v5.5c0 4.6 3.1 8.1 7.5 9.5 4.4-1.4 7.5-4.9 7.5-9.5V6z"/><path d="M12 8v5M12 16h.01"/>',
    "ban": '<circle cx="12" cy="12" r="9"/><path d="m5.7 5.7 12.6 12.6"/>',
    "sliders": '<path d="M4 6.5h9M17 6.5h3M4 12h3M11 12h9M4 17.5h11M19 17.5h1"/><circle cx="15" cy="6.5" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="17.5" r="2"/>',
    "phone": '<rect x="6.5" y="2.5" width="11" height="19" rx="2.5"/><path d="M10.5 18.5h3"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/><path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M21.5 20a6.5 6.5 0 0 0-3.5-5.8"/>',
    "pause": '<rect x="6.5" y="5" width="3.5" height="14" rx="1"/><rect x="14" y="5" width="3.5" height="14" rx="1"/>',
    "play": '<path d="M7.5 4.8v14.4a.8.8 0 0 0 1.2.7l11.3-7.2a.8.8 0 0 0 0-1.4L8.7 4.1a.8.8 0 0 0-1.2.7Z"/>',
    "logout": '<path d="M14.5 4h3.5a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3.5"/><path d="m10 16-4-4 4-4M6 12h10"/>',
    "upload": '<path d="M12 15.5V4M7.5 8.5 12 4l4.5 4.5"/><path d="M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V15"/>',
    "download": '<path d="M12 4v11.5M7.5 11 12 15.5l4.5-4.5"/><path d="M4 15v3.5A2.5 2.5 0 0 0 6.5 21h11a2.5 2.5 0 0 0 2.5-2.5V15"/>',
    "trash": '<path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "check": '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    "alert": '<path d="M10.3 4.2 2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4M12 17h.01"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.5h.01"/>',
    "send": '<path d="M4.5 12 20 4.5 15.5 20l-3.5-6.5z"/><path d="M12 13.5 20 4.5"/>',
    "run": '<path d="M20 12a8 8 0 1 1-2.4-5.7L20 8.5"/><path d="M20 3.5v5h-5"/>',
    "stop": '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>',
    "menu": '<path d="M4 7h16M4 12h16M4 17h16"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.2 2"/>',
    "copy": '<rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/><path d="M15.5 8.5V6a2.5 2.5 0 0 0-2.5-2.5H6A2.5 2.5 0 0 0 3.5 6v7A2.5 2.5 0 0 0 6 15.5h2.5"/>',
    "key": '<circle cx="8" cy="15" r="4.5"/><path d="m11.2 11.8 8.3-8.3M16 7l2.5 2.5M18.5 4.5 20.5 6.5"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "sparkle": '<path d="M12 3.5 13.8 10.2 20.5 12l-6.7 1.8L12 20.5l-1.8-6.7L3.5 12l6.7-1.8z"/>',
    "wifi-off": '<path d="M3 3l18 18"/><path d="M8.5 16.5a5 5 0 0 1 7 0M5 12.9a10 10 0 0 1 4.2-2.4M19 12.9a10 10 0 0 0-3-2M2 9.3a15 15 0 0 1 4.3-2.6M22 9.3A15 15 0 0 0 11 5.1"/><path d="M12 20h.01"/>',
}


def icon(name: str, size: int = 18) -> str:
    return (
        f'<svg class="i" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[name]}</svg>'
    )


# Jeli's own mark, a lemur, shown until its WhatsApp profile picture is available.
LEMUR = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true"><circle cx="32" cy="32" r="32" fill="#2a2c32"/>
<path d="M12 21 17 8l9 11z" fill="#cfcbc1"/><path d="M52 21 47 8l-9 11z" fill="#cfcbc1"/>
<ellipse cx="32" cy="35" rx="19" ry="17" fill="#f1eee7"/>
<path d="M32 18c-3 0-4 4-4 8h8c0-4-1-8-4-8z" fill="#2a2c32"/>
<ellipse cx="23.5" cy="32" rx="7.4" ry="7.8" fill="#2a2c32"/><ellipse cx="40.5" cy="32" rx="7.4" ry="7.8" fill="#2a2c32"/>
<circle cx="23.5" cy="32" r="4.8" fill="#f5b301"/><circle cx="40.5" cy="32" r="4.8" fill="#f5b301"/>
<circle cx="24" cy="32.4" r="2.1" fill="#141518"/><circle cx="41" cy="32.4" r="2.1" fill="#141518"/>
<path d="M29 42.5q3 3.2 6 0z" fill="#2a2c32"/></svg>"""

NAV = [
    ("home", "Overview", "/dashboard", "home"),
    ("try", "Try Jeli", "/dashboard/try", "chat"),
    ("questions", "Questions", "/dashboard/questions", "question"),
    ("deadlines", "Deadlines", "/dashboard/deadlines", "calendar"),
    ("knowledge", "Knowledge", "/dashboard/knowledge", "book"),
    ("activities", "Activities", "/dashboard/activities", "activity"),
    ("watch", "Watchlist", "/dashboard/watchlist", "shield"),
    ("exceptions", "Exceptions", "/dashboard/exceptions", "ban"),
    ("settings", "Settings", "/dashboard/settings", "sliders"),
    ("whatsapp", "WhatsApp", "/dashboard/whatsapp", "phone"),
    ("team", "Team", "/dashboard/team", "users"),
]


# --- Time, in the reader's own time zone (converted in the browser) -------------------------------


def when(moment: datetime | None, style: str = "datetime") -> str:
    """A moment, shown in the reader's time zone: "datetime", "date", "time" or "ago"."""
    if moment is None:
        return "—"
    moment = moment.astimezone(timezone.utc)
    fallback = {"date": f"{moment:%a %d %b}", "time": f"{moment:%H:%M} GMT"}.get(style, f"{moment:%a %d %b, %H:%M} GMT")
    return f'<time datetime="{moment.isoformat()}" data-style="{style}">{fallback}</time>'


def duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def in_words(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "about a minute"
    if seconds < 3600:
        return f"{round(seconds / 60)} minutes"
    if seconds < 36 * 3600:
        hours = round(seconds / 3600)
        return f"{hours} hour{'s' if hours > 1 else ''}"
    days = round(seconds / 86400)
    return f"{days} days"


# --- Components ---------------------------------------------------------------------------------


def card(title: str, body: str, *, description: str = "", actions: str = "", icon_name: str = "", cls: str = "") -> str:
    head_icon = f'<span class="card-icon">{icon(icon_name)}</span>' if icon_name else ""
    desc = f'<p class="card-desc">{esc(description)}</p>' if description else ""
    return (
        f'<section class="card {cls}"><header class="card-head">{head_icon}<div class="card-titles">'
        f"<h2>{esc(title)}</h2>{desc}</div><div class='card-actions'>{actions}</div></header>"
        f'<div class="card-body">{body}</div></section>'
    )


def pill(level: str, text: str) -> str:
    symbol = {"good": "check", "warn": "alert", "bad": "alert", "neutral": "clock", "info": "info"}[level]
    return f'<span class="pill {level}">{icon(symbol, 14)}{esc(text)}</span>'


def stat(label: str, value: str, sub: str = "", tone: str = "") -> str:
    return (
        f'<div class="stat {tone}"><span class="stat-label">{esc(label)}</span>'
        f'<span class="stat-value">{esc(value)}</span><span class="stat-sub">{esc(sub)}</span></div>'
    )


def button(label: str, *, kind: str = "", icon_name: str = "", type_: str = "submit", attrs: str = "") -> str:
    glyph = icon(icon_name, 16) if icon_name else ""
    return f'<button type="{type_}" class="btn {kind}" {attrs}>{glyph}<span>{esc(label)}</span></button>'


def hidden(name: str, value: str) -> str:
    return f'<input type="hidden" name="{esc(name)}" value="{esc(value)}">'


def form(action: str, csrf: str, inner: str, *, confirm: str = "", cls: str = "", upload: bool = False) -> str:
    extra = f' data-confirm="{esc(confirm)}"' if confirm else ""
    encoding = ' enctype="multipart/form-data"' if upload else ""
    return f'<form method="post" action="{esc(action)}" class="{cls}"{encoding}{extra}>{hidden("csrf", csrf)}{inner}</form>'


def switch(action: str, csrf: str, *, on: bool, name: str, label: str, fields: str = "") -> str:
    """An on/off switch that saves at once."""
    return form(
        action,
        csrf,
        f"{fields}{hidden(name, 'off' if on else 'on')}"
        f'<button type="submit" class="switch {"on" if on else ""}" role="switch" aria-checked="{str(on).lower()}" '
        f'aria-label="{esc(label)}"><span class="knob"></span></button>',
        cls="inline",
    )


def empty(text: str, icon_name: str = "info") -> str:
    return f'<div class="empty">{icon(icon_name, 22)}<p>{esc(text)}</p></div>'


def table(headers: list[str], rows: list[list[str]], *, numeric: set[int] = frozenset(), empty_text: str = "Nothing yet") -> str:
    """Rows are already-escaped HTML cells."""
    if not rows:
        return empty(empty_text)
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{esc(h)}</th>' for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f'<td class="{"num" if i in numeric else ""}">{cell}</td>' for i, cell in enumerate(row)) + "</tr>"
        for row in rows
    )
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def notice(level: str, text: str, action: str = "") -> str:
    symbol = {"good": "check", "warn": "alert", "bad": "alert", "info": "info"}[level]
    return f'<div class="notice {level}">{icon(symbol)}<div class="notice-text">{text}</div>{action}</div>'


# --- Page -----------------------------------------------------------------------------------------


def layout(
    *,
    title: str,
    subtitle: str,
    active: str,
    member: str,
    csrf: str,
    body: str,
    paused: bool,
    path: str,
    flash: tuple[str, str] | None = None,
    live: bool = False,
) -> str:
    nav = "".join(
        f'<a href="{href}" class="nav-item{" active" if key == active else ""}"'
        f'{" aria-current=page" if key == active else ""}>{icon(glyph)}<span>{label}</span></a>'
        for key, label, href, glyph in NAV
    )
    state = (
        pill("warn", "Paused: Jeli answers nobody")
        if paused
        else pill("good", "Answering")
    )
    toggle = form(
        "/dashboard/pause",
        csrf,
        hidden("action", "resume" if paused else "pause")
        + hidden("next", path)
        + (button("Resume Jeli", kind="primary", icon_name="play") if paused else button("Pause Jeli", icon_name="pause")),
        confirm="" if paused else "Pause Jeli? It will answer nobody and post nothing until you resume it. It keeps remembering the groups.",
        cls="inline",
    )
    flash_html = notice(flash[0], esc(flash[1])) if flash else ""
    live_dot = '<span class="live-dot" id="live-status" data-state="on"><i></i><span>Live</span></span>' if live else ""
    content = f'<div id="live-body" data-live>{body}</div>' if live else body
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>{esc(title)} · Jeli</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="icon" href="/dashboard/avatar">
<style>{CSS}</style></head>
<body>
<input type="checkbox" id="nav-toggle" class="nav-toggle" aria-hidden="true">
<aside class="sidebar">
  <div class="brand"><img src="/dashboard/avatar" alt="" class="avatar"><div><b>Jeli</b><span>Group memory</span></div>
  <label for="nav-toggle" class="nav-close" aria-label="Close menu">×</label></div>
  <nav>{nav}</nav>
  <div class="sidebar-foot"><div class="me"><span class="me-initial">{esc(member[:1].upper())}</span><span>{esc(member.capitalize())}</span></div>
  {form("/logout", csrf, button("Sign out", kind="ghost small", icon_name="logout"), cls="inline")}</div>
</aside>
<div class="main">
  <header class="topbar">
    <label for="nav-toggle" class="menu-button" aria-label="Menu">{icon("menu", 22)}</label>
    <div class="titles"><h1>{esc(title)}</h1><p>{esc(subtitle)}</p></div>
    <div class="top-actions"><span class="top-state">{state}</span>{live_dot}{toggle}</div>
  </header>
  <main class="content">{flash_html}{content}</main>
</div>
<script>{JS}</script>
</body></html>"""


def login_page(error: str = "", next_path: str = "/dashboard") -> str:
    message = notice("bad", esc(error)) if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Sign in · Jeli</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head>
<body class="login-body"><main class="login">
  <div class="login-brand"><span class="login-avatar">{LEMUR}</span><h1>Jeli</h1><p>The team's control panel</p></div>
  {message}
  <form method="post" action="/login" class="login-form">
    {hidden("next", next_path)}
    <label>Name<input name="name" autocomplete="username" required autofocus></label>
    <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
    {button("Sign in", kind="primary wide")}
  </form>
  <p class="login-foot">Only Jeli's team can sign in. Lost your password? Ask a teammate.</p>
</main></body></html>"""


CSS = """
:root {
  color-scheme: light;
  --page: #f5f4f0; --surface: #ffffff; --surface-2: #faf9f6; --border: #e6e3db; --border-strong: #d6d2c7;
  --ink: #1b1c1f; --ink-2: #55575e; --muted: #8a8c93;
  --brand: #f5b301; --brand-ink: #1b1c1f; --brand-soft: #fff4d6; --brand-text: #8a5d00;
  --side: #17181c; --side-2: #212329; --side-ink: #c8cad1; --side-muted: #7f828b;
  --good: #16794a; --good-bg: #e6f4ec; --warn: #8a5d00; --warn-bg: #fff3d6; --bad: #b3261e; --bad-bg: #fdecea;
  --info: #2458b3; --info-bg: #e9f0fc; --neutral-bg: #efeee9;
  --series-1: #2a78d6; --series-2: #eb6834; --series-3: #1baf7a; --grid: #ebe8e0;
  --shadow: 0 1px 2px rgba(20,20,20,.04), 0 2px 8px rgba(20,20,20,.04);
  --radius: 14px;
  --wa-bg: #efeae2; --wa-dots: rgba(11,20,26,.05); --wa-in: #ffffff; --wa-out: #d9fdd3; --wa-ink: #111b21; --wa-muted: #667781;
  --wa-reply: rgba(11,20,26,.05); --wa-bar: rgba(11,20,26,.18); --wa-head: #008069; --wa-head-ink: #ffffff; --wa-compose: #f0f2f5; --wa-system: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0f1013; --surface: #17181c; --surface-2: #1c1d22; --border: #2a2c32; --border-strong: #3a3c43;
    --ink: #f2f2f0; --ink-2: #b9bbc2; --muted: #80838c;
    --brand-soft: #3a2f10; --brand-text: #f5c542;
    --side: #0b0c0e; --side-2: #1a1b20;
    --good: #4cc38a; --good-bg: #13301f; --warn: #f5c542; --warn-bg: #362b0f; --bad: #ff7b72; --bad-bg: #3a1614;
    --info: #7aa7ff; --info-bg: #15243f; --neutral-bg: #24262c;
    --series-1: #3987e5; --series-2: #d95926; --series-3: #199e70; --grid: #2a2c32;
    --shadow: none;
    --wa-bg: #0b141a; --wa-dots: rgba(255,255,255,.035); --wa-in: #202c33; --wa-out: #005c4b; --wa-ink: #e9edef; --wa-muted: #8696a0;
    --wa-reply: rgba(255,255,255,.07); --wa-bar: rgba(255,255,255,.25); --wa-head: #202c33; --wa-head-ink: #e9edef; --wa-compose: #111b21; --wa-system: #182229;
  }
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--page); color: var(--ink); font: 14px/1.5 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: inherit; }
h1, h2, h3 { margin: 0; letter-spacing: -0.01em; }
.i { flex: none; }
.nav-toggle { display: none; }

/* Sidebar */
.sidebar { position: fixed; inset: 0 auto 0 0; width: 248px; background: var(--side); color: var(--side-ink); display: flex; flex-direction: column; padding: 18px 12px; z-index: 20; }
.brand { display: flex; align-items: center; gap: 12px; padding: 4px 8px 20px; }
.brand b { display: block; color: #fff; font-size: 17px; }
.brand span { color: var(--side-muted); font-size: 12px; }
.avatar { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; background: #2a2c32; box-shadow: 0 0 0 2px rgba(245,179,1,.55); }
.nav-close { display: none; margin-left: auto; font-size: 26px; cursor: pointer; color: var(--side-muted); }
nav { display: flex; flex-direction: column; gap: 2px; overflow-y: auto; }
.nav-item { display: flex; align-items: center; gap: 12px; padding: 9px 12px; border-radius: 10px; text-decoration: none; color: var(--side-ink); font-weight: 500; position: relative; }
.nav-item:hover { background: var(--side-2); color: #fff; }
.nav-item.active { background: var(--side-2); color: #fff; }
.nav-item.active::before { content: ""; position: absolute; left: -12px; top: 8px; bottom: 8px; width: 3px; border-radius: 0 3px 3px 0; background: var(--brand); }
.nav-item.active .i { color: var(--brand); }
.sidebar-foot { margin-top: auto; border-top: 1px solid #25272d; padding: 14px 8px 0; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.me { display: flex; align-items: center; gap: 10px; font-weight: 500; color: #fff; }
.me-initial { width: 30px; height: 30px; border-radius: 50%; background: var(--brand); color: var(--brand-ink); display: grid; place-items: center; font-weight: 700; font-size: 13px; }
.sidebar .btn.ghost { color: var(--side-ink); }
.sidebar .btn.ghost:hover { background: var(--side-2); color: #fff; }

/* Main */
.main { margin-left: 248px; min-height: 100vh; }
.topbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 16px; padding: 18px 32px; background: color-mix(in srgb, var(--page) 88%, transparent); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border); }
.titles { flex: 1; min-width: 0; }
.titles h1 { font-size: 22px; font-weight: 700; }
.titles p { margin: 2px 0 0; color: var(--ink-2); }
.top-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; justify-content: flex-end; }
.menu-button { display: none; cursor: pointer; color: var(--ink); }
.content { padding: 28px 32px 56px; max-width: 1240px; display: grid; gap: 20px; }

/* Cards */
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); box-shadow: var(--shadow); min-width: 0; }
.card-head { display: flex; align-items: flex-start; gap: 12px; padding: 18px 20px 0; }
.card-icon { width: 34px; height: 34px; border-radius: 10px; background: var(--brand-soft); color: var(--brand-text); display: grid; place-items: center; flex: none; }
.card-titles { flex: 1; min-width: 0; }
.card-head h2 { font-size: 15px; font-weight: 600; }
.card-desc { margin: 3px 0 0; color: var(--ink-2); font-size: 13px; }
.card-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.card-body { padding: 16px 20px 20px; }
.grid { display: grid; gap: 20px; }
.grid.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.grid.side { grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); }

/* Hero */
.hero { display: flex; align-items: center; gap: 20px; padding: 22px 24px; }
.hero .avatar { width: 64px; height: 64px; }
.hero h2 { font-size: 20px; font-weight: 700; }
.hero p { margin: 4px 0 0; color: var(--ink-2); }
.hero .hero-text { flex: 1; min-width: 0; }
.hero.paused { background: linear-gradient(0deg, var(--warn-bg), var(--warn-bg)); border-color: color-mix(in srgb, var(--warn) 30%, var(--border)); }

/* Stats */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.stat { display: flex; flex-direction: column; gap: 2px; padding: 14px 16px; border: 1px solid var(--border); border-radius: 12px; background: var(--surface-2); min-width: 0; }
.stat-label { color: var(--ink-2); font-size: 13px; }
.stat-value { font-size: 26px; font-weight: 700; letter-spacing: -0.02em; line-height: 1.2; }
.stat-sub { color: var(--muted); font-size: 12px; }

/* Pills, notices */
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px 4px 8px; border-radius: 999px; font-size: 12.5px; font-weight: 600; white-space: nowrap; }
.pill.good { background: var(--good-bg); color: var(--good); }
.pill.warn { background: var(--warn-bg); color: var(--warn); }
.pill.bad { background: var(--bad-bg); color: var(--bad); }
.pill.info { background: var(--info-bg); color: var(--info); }
.pill.neutral { background: var(--neutral-bg); color: var(--ink-2); }
.notice { display: flex; align-items: flex-start; gap: 12px; padding: 12px 16px; border-radius: 12px; border: 1px solid transparent; }
.notice .i { margin-top: 1px; }
.notice-text { flex: 1; }
.notice.good { background: var(--good-bg); color: var(--good); }
.notice.warn { background: var(--warn-bg); color: var(--warn); }
.notice.bad { background: var(--bad-bg); color: var(--bad); }
.notice.info { background: var(--info-bg); color: var(--info); }
.notice a { font-weight: 600; }
.attention { display: grid; gap: 10px; }

/* Buttons */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: 38px; padding: 0 14px; border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface); color: var(--ink); font: inherit; font-weight: 600; cursor: pointer; text-decoration: none; white-space: nowrap; }
.btn:hover { background: var(--surface-2); border-color: var(--muted); }
.btn:focus-visible, .switch:focus-visible, input:focus-visible, select:focus-visible, textarea:focus-visible, .nav-item:focus-visible { outline: 3px solid color-mix(in srgb, var(--brand) 55%, transparent); outline-offset: 2px; }
.btn.primary { background: var(--brand); border-color: var(--brand); color: var(--brand-ink); }
.btn.primary:hover { filter: brightness(0.96); }
.btn.danger { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 35%, var(--border)); }
.btn.danger:hover { background: var(--bad-bg); }
.btn.ghost { background: transparent; border-color: transparent; }
.btn.small { height: 32px; padding: 0 10px; font-size: 13px; }
.btn.wide { width: 100%; height: 44px; }
.btn[disabled] { opacity: .5; cursor: not-allowed; }
form.inline { display: inline-flex; margin: 0; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }

/* Switch */
.switch { width: 44px; height: 26px; border-radius: 999px; border: none; background: var(--border-strong); position: relative; cursor: pointer; padding: 0; flex: none; transition: background .15s; }
.switch .knob { position: absolute; top: 3px; left: 3px; width: 20px; height: 20px; border-radius: 50%; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.25); transition: transform .15s; }
.switch.on { background: var(--good); }
.switch.on .knob { transform: translateX(18px); }

/* Rows of settings */
.rows { display: grid; }
.row { display: flex; align-items: center; gap: 16px; padding: 14px 0; border-top: 1px solid var(--border); }
.row:first-child { border-top: none; padding-top: 0; }
.row-text { flex: 1; min-width: 0; }
.row-text b { display: block; font-weight: 600; }
.row-text span { color: var(--ink-2); font-size: 13px; }
.row-side { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }

/* Forms */
label { display: grid; gap: 6px; font-weight: 500; font-size: 13px; color: var(--ink-2); }
input, select, textarea { font: inherit; color: var(--ink); background: var(--surface); border: 1px solid var(--border-strong); border-radius: 10px; padding: 9px 12px; min-height: 40px; width: 100%; }
textarea { min-height: 96px; resize: vertical; line-height: 1.5; }
input[type="file"] { padding: 7px; }
input[type="checkbox"], input[type="radio"] { width: 18px; height: 18px; min-height: 0; accent-color: var(--brand); }
.fields { display: grid; gap: 14px; }
.fields.two { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.fields.four { grid-template-columns: 2fr 1fr 1fr 1fr; }
.check { display: flex; align-items: center; gap: 10px; font-weight: 500; color: var(--ink); }
.hint { color: var(--muted); font-size: 12.5px; margin: 0; }
.segmented { display: inline-flex; border: 1px solid var(--border-strong); border-radius: 10px; overflow: hidden; }
.segmented label { display: block; }
.segmented input { position: absolute; opacity: 0; width: 1px; height: 1px; }
.segmented span { display: block; padding: 8px 14px; cursor: pointer; font-weight: 600; color: var(--ink-2); border-left: 1px solid var(--border-strong); }
.segmented label:first-child span { border-left: none; }
.segmented input:checked + span { background: var(--brand); color: var(--brand-ink); }
.segmented input:focus-visible + span { outline: 3px solid color-mix(in srgb, var(--brand) 55%, transparent); outline-offset: -3px; }

/* Tables */
.table-wrap { overflow-x: auto; margin: 0 -4px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th { text-align: left; font-size: 11.5px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); font-weight: 600; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 11px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--surface-2); }
.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.muted { color: var(--muted); }
.small { font-size: 12.5px; }
.nowrap { white-space: nowrap; }
.strong { font-weight: 600; }

/* Tags (lists of names) */
.tags { display: flex; flex-wrap: wrap; gap: 8px; }
.tag { display: inline-flex; align-items: center; gap: 4px; padding: 4px 4px 4px 12px; border-radius: 999px; background: var(--neutral-bg); font-weight: 500; }
.tag .btn { height: 26px; width: 26px; padding: 0; border-radius: 50%; }

/* Empty */
.empty { display: flex; align-items: center; gap: 12px; padding: 18px; border: 1px dashed var(--border-strong); border-radius: 12px; color: var(--ink-2); }
.empty p { margin: 0; }

/* Activities */
.activity { display: grid; grid-template-columns: auto 1fr auto; gap: 16px; align-items: start; padding: 16px 0; border-top: 1px solid var(--border); }
.activity:first-child { border-top: none; padding-top: 0; }
.activity h3 { font-size: 14.5px; font-weight: 600; }
.activity p { margin: 2px 0 0; color: var(--ink-2); font-size: 13px; }
.activity .meta { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 8px; color: var(--ink-2); font-size: 12.5px; }
.activity .meta span { display: inline-flex; align-items: center; gap: 6px; }
.activity-side { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
.schedule { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-top: 10px; }
.schedule select, .schedule input { width: auto; min-height: 34px; padding: 5px 10px; }

/* Try Jeli */
.chat { display: flex; flex-direction: column; gap: 14px; min-height: 280px; max-height: 60vh; overflow-y: auto; padding: 16px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 12px; }
.bubble { max-width: 82%; padding: 10px 14px; border-radius: 14px; white-space: pre-wrap; word-wrap: break-word; line-height: 1.5; }
.bubble.me { align-self: flex-end; background: var(--brand-soft); border-bottom-right-radius: 4px; }
.bubble.jeli { align-self: flex-start; background: var(--surface); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
.bubble.silent { align-self: flex-start; color: var(--muted); font-style: italic; background: none; border: 1px dashed var(--border-strong); }
.bubble .meta { display: block; margin-top: 6px; font-size: 11.5px; color: var(--muted); white-space: normal; }
.composer { display: grid; gap: 10px; margin-top: 14px; }
.composer-row { display: flex; gap: 10px; align-items: flex-end; }
.composer-row textarea { min-height: 48px; height: 48px; }
.chips { display: flex; flex-wrap: wrap; gap: 8px; }
.chip { border: 1px solid var(--border-strong); background: var(--surface); border-radius: 999px; padding: 5px 12px; font: inherit; font-size: 13px; cursor: pointer; color: var(--ink-2); }
.chip:hover { border-color: var(--brand); color: var(--ink); }

/* Chart */
.chart-wrap { overflow-x: auto; }
.chart { width: 100%; min-width: 420px; height: auto; display: block; }
.chart .grid-line { stroke: var(--grid); stroke-width: 1; }
.chart .base { stroke: var(--border-strong); stroke-width: 1; }
.chart .tick { fill: var(--muted); font-size: 11px; }
.chart .total { fill: var(--ink-2); font-size: 11px; font-weight: 600; }
.chart .s1 { fill: var(--series-1); } .chart .s2 { fill: var(--series-2); } .chart .s3 { fill: var(--series-3); }
.chart .hit { fill: transparent; }
.chart .col:hover .hit { fill: var(--grid); opacity: .5; }
.chart .empty-note { fill: var(--muted); font-size: 13px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0 0 10px; padding: 0; list-style: none; color: var(--ink-2); font-size: 13px; }
.legend li { display: inline-flex; align-items: center; gap: 8px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; }
.swatch.s1 { background: var(--series-1); } .swatch.s2 { background: var(--series-2); } .swatch.s3 { background: var(--series-3); }
details summary { cursor: pointer; color: var(--ink-2); font-weight: 500; margin-top: 10px; }

/* QR */
.qr { display: grid; place-items: center; padding: 16px; background: #fff; border-radius: 12px; border: 1px solid var(--border); width: fit-content; }
.qr img { width: 260px; height: 260px; image-rendering: pixelated; }
.steps { margin: 0; padding-left: 20px; display: grid; gap: 6px; }

/* Login */
.login-body { min-height: 100vh; display: grid; place-items: center; padding: 16px; background: radial-gradient(1200px 500px at 50% -10%, var(--brand-soft), var(--page)); }
.login { width: 100%; max-width: 380px; background: var(--surface); border: 1px solid var(--border); border-radius: 18px; padding: 32px 28px; box-shadow: 0 10px 40px rgba(0,0,0,.08); display: grid; gap: 18px; }
.login-brand { text-align: center; display: grid; justify-items: center; gap: 4px; }
.login-avatar svg { width: 72px; height: 72px; }
.login-brand h1 { font-size: 24px; }
.login-brand p { margin: 0; color: var(--ink-2); }
.login-form { display: grid; gap: 14px; }
.login-foot { margin: 0; text-align: center; color: var(--muted); font-size: 12.5px; }

/* Live */
.live-dot { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--good); }
.live-dot i { width: 8px; height: 8px; border-radius: 50%; background: var(--good); box-shadow: 0 0 0 0 color-mix(in srgb, var(--good) 60%, transparent); animation: pulse 2s infinite; }
.live-dot[data-state="off"] { color: var(--muted); }
.live-dot[data-state="off"] i { background: var(--muted); animation: none; }
@keyframes pulse { 0% { box-shadow: 0 0 0 0 color-mix(in srgb, var(--good) 55%, transparent); } 70% { box-shadow: 0 0 0 7px transparent; } 100% { box-shadow: 0 0 0 0 transparent; } }
#live-body { display: grid; gap: 20px; }
#live-body.flash { animation: flash 1.2s ease-out; }
@keyframes flash { from { filter: brightness(1.04); } to { filter: none; } }
@media (prefers-reduced-motion: reduce) { .live-dot i, #live-body.flash { animation: none; } }

/* The live feed */
.feed { display: grid; }
.feed-item { display: grid; grid-template-columns: auto 1fr auto; gap: 12px; padding: 11px 0; border-top: 1px solid var(--border); align-items: start; }
.feed-item:first-child { border-top: none; padding-top: 0; }
.feed-where { font-size: 12px; color: var(--muted); }
.feed-text { overflow-wrap: anywhere; }
.feed-icon { width: 30px; height: 30px; border-radius: 50%; display: grid; place-items: center; background: var(--neutral-bg); color: var(--ink-2); }
.feed-icon.test { background: var(--brand-soft); color: var(--brand-text); }
.today { display: flex; flex-wrap: wrap; gap: 8px 22px; color: var(--ink-2); font-size: 13px; margin-top: 8px; }
.today b { color: var(--ink); }

/* WhatsApp, as on a phone */
.wa { border: 1px solid var(--border); border-radius: 14px; overflow: hidden; background: var(--wa-bg); }
.wa-head { display: flex; align-items: center; gap: 12px; padding: 10px 14px; background: var(--wa-head); color: var(--wa-head-ink); flex-wrap: wrap; }
.wa-avatar { width: 38px; height: 38px; border-radius: 50%; object-fit: cover; background: #2a2c32; }
.wa-who { display: grid; line-height: 1.25; flex: 1; min-width: 120px; }
.wa-who span { font-size: 12.5px; opacity: .8; }
.wa-where { display: flex; align-items: center; gap: 8px; color: inherit; font-size: 12.5px; }
.wa-where select { width: auto; min-height: 32px; padding: 4px 8px; font-size: 13px; }
.wa-chat { min-height: 340px; max-height: 62vh; overflow-y: auto; padding: 16px 5%; display: flex; flex-direction: column; gap: 6px;
  background-color: var(--wa-bg); background-image: radial-gradient(var(--wa-dots) 1px, transparent 1px); background-size: 18px 18px; }
.wa-msg { max-width: min(78%, 560px); padding: 6px 9px 6px; border-radius: 8px; font-size: 14.2px; line-height: 1.42; color: var(--wa-ink); box-shadow: 0 1px .5px rgba(11,20,26,.13); position: relative; overflow-wrap: anywhere; }
.wa-msg.in { align-self: flex-start; background: var(--wa-in); border-top-left-radius: 0; }
.wa-msg.out { align-self: flex-end; background: var(--wa-out); border-top-right-radius: 0; }
.wa-sender { font-size: 12.8px; font-weight: 600; color: #d97706; margin-bottom: 2px; }
.wa-reply { display: grid; gap: 1px; background: var(--wa-reply); border-left: 4px solid #06cf9c; border-radius: 6px; padding: 5px 8px; margin: 2px 0 5px; font-size: 13px; }
.wa-reply b { color: #06a37e; font-weight: 600; }
.wa-reply span { color: var(--wa-muted); display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.wa-quote { border-left: 3px solid var(--wa-bar); padding: 1px 0 1px 9px; margin: 3px 0; color: var(--wa-muted); }
.wa-li { padding-left: 2px; }
.wa-gap { height: 6px; }
.wa-mention { color: #027eb5; }
.wa-mono, .wa-msg code { font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; background: var(--wa-reply); padding: 0 3px; border-radius: 3px; }
.wa-msg a { color: #027eb5; }
.wa-meta { font-size: 11px; color: var(--wa-muted); text-align: right; margin-top: 2px; }
.wa-ticks { color: #53bdeb; letter-spacing: -3px; margin-left: 2px; }
.wa-note { font-size: 11.5px; color: var(--wa-muted); font-style: italic; margin-top: 3px; border-top: 1px dashed var(--wa-bar); padding-top: 3px; }
.wa-system { align-self: center; background: var(--wa-system); color: var(--wa-muted); font-size: 12.5px; padding: 5px 12px; border-radius: 8px; max-width: 85%; text-align: center; box-shadow: 0 1px .5px rgba(11,20,26,.1); }
.wa-compose { display: grid; gap: 10px; padding: 12px 14px; background: var(--wa-compose); }
.wa-row { display: flex; gap: 10px; align-items: flex-end; }
.wa-row textarea { min-height: 44px; height: 44px; border-radius: 22px; padding: 11px 16px; border: none; background: var(--surface); }
.wa-send { width: 44px; height: 44px; border-radius: 50%; border: none; background: #00a884; color: #fff; display: grid; place-items: center; cursor: pointer; flex: none; }
.wa-send:hover { background: #008f72; }

/* Phone and tablet */
@media (max-width: 1100px) {
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid.three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .grid.side { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 860px) {
  .sidebar { transform: translateX(-100%); transition: transform .2s; width: 270px; box-shadow: 0 0 40px rgba(0,0,0,.3); }
  .nav-toggle:checked ~ .sidebar { transform: none; }
  .nav-close { display: block; }
  .main { margin-left: 0; }
  .menu-button { display: grid; }
  .topbar { padding: 12px 16px; flex-wrap: wrap; }
  .titles h1 { font-size: 18px; }
  .titles p { font-size: 13px; }
  .top-actions { width: 100%; justify-content: space-between; }
  .content { padding: 16px 16px 40px; gap: 16px; }
  .grid.two, .grid.three, .fields.two, .fields.four { grid-template-columns: minmax(0, 1fr); }
  .card-head { padding: 16px 16px 0; flex-wrap: wrap; }
  .card-body { padding: 14px 16px 16px; }
  .hero { flex-wrap: wrap; padding: 18px; }
  .row { flex-wrap: wrap; }
  .row-side { width: 100%; justify-content: flex-start; }
  .activity { grid-template-columns: auto 1fr; }
  .activity-side { grid-column: 1 / -1; justify-content: flex-start; }
  .bubble { max-width: 92%; }
}
@media (max-width: 480px) {
  .stats { grid-template-columns: minmax(0, 1fr); }
}
"""

JS = r"""
(function () {
  const pad = n => String(n).padStart(2, '0');
  const days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function ago(d) {
    const s = Math.round((Date.now() - d) / 1000), future = s < 0, a = Math.abs(s);
    let t = a < 60 ? 'a few seconds' : a < 3600 ? Math.round(a / 60) + ' min' : a < 86400 ? Math.round(a / 3600) + ' h' : Math.round(a / 86400) + ' days';
    return future ? 'in ' + t : t + ' ago';
  }
  function times(root) {
    root.querySelectorAll('time[datetime]').forEach(el => {
      const d = new Date(el.getAttribute('datetime'));
      if (isNaN(d)) return;
      const style = el.dataset.style, date = days[d.getDay()] + ' ' + d.getDate() + ' ' + months[d.getMonth()], clock = pad(d.getHours()) + ':' + pad(d.getMinutes());
      el.textContent = style === 'ago' ? ago(d) : style === 'date' ? date : style === 'time' ? clock : date + ', ' + clock;
      el.title = d.toLocaleString();
    });
  }
  function enhance(root) {
    times(root);
    root.querySelectorAll('form[data-confirm]').forEach(f => f.addEventListener('submit', e => { if (!confirm(f.dataset.confirm)) e.preventDefault(); }));
    root.querySelectorAll('form').forEach(f => f.addEventListener('input', () => { f.dataset.dirty = '1'; }));
    root.querySelectorAll('[data-copy]').forEach(b => b.addEventListener('click', () => {
      const text = document.getElementById(b.dataset.copy).innerText;
      navigator.clipboard.writeText(text).then(() => { b.querySelector('span').textContent = 'Copied'; });
    }));
  }
  enhance(document);
  setInterval(() => times(document), 30000);

  // Live: the page asks every few seconds whether anything changed (a cheap check), and swaps in
  // the fresh content only then — never while someone is typing in it.
  const live = document.getElementById('live-body'), dot = document.getElementById('live-status');
  if (live && dot) {
    let etag = null, busy = false;
    const say = (state, text) => { dot.dataset.state = state; dot.querySelector('span').textContent = text; };
    async function poll() {
      if (document.hidden || busy) return;
      const typing = document.activeElement && live.contains(document.activeElement) && document.activeElement.matches('input, textarea, select');
      if (typing || live.querySelector('form[data-dirty]')) return;
      busy = true;
      try {
        const headers = { 'X-Live': '1' };
        if (etag) headers['If-None-Match'] = etag;
        const r = await fetch(location.href, { headers, cache: 'no-store' });
        if (r.status === 200) {
          const changed = etag !== null;
          etag = r.headers.get('ETag');
          const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
          const fresh = doc.getElementById('live-body'), top = doc.querySelector('.top-state');
          if (fresh && changed) { live.innerHTML = fresh.innerHTML; enhance(live); live.classList.remove('flash'); void live.offsetWidth; live.classList.add('flash'); }
          if (top) document.querySelector('.top-state').innerHTML = top.innerHTML;
        }
        if (r.status === 200 || r.status === 304) say('on', 'Live');
        else say('off', 'Reconnecting…');
      } catch (e) { say('off', 'Reconnecting…'); }
      busy = false;
    }
    poll();
    setInterval(poll, 4000);
    document.addEventListener('visibilitychange', poll);
  }

  // WhatsApp's own formatting, for Jeli's replies shown as on a phone.
  const esc = t => String(t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  function inline(t) {
    return t
      .replace(/```([\s\S]+?)```/g, '<code class="wa-mono">$1</code>')
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(>])\*([^*\n]+?)\*(?=[\s).,!?:;<]|$)/g, '$1<b>$2</b>')
      .replace(/(^|[\s(>])_([^_\n]+?)_(?=[\s).,!?:;<]|$)/g, '$1<i>$2</i>')
      .replace(/(^|[\s(>])~([^~\n]+?)~(?=[\s).,!?:;<]|$)/g, '$1<s>$2</s>')
      .replace(/(^|\s)@(\d{5,})/g, '$1<span class="wa-mention">@$2</span>');
  }
  function format(text) {
    const lines = esc(text).split('\n'); let html = '', quote = [];
    const flush = () => { if (quote.length) { html += '<div class="wa-quote">' + quote.map(inline).join('<br>') + '</div>'; quote = []; } };
    for (const line of lines) {
      if (line.startsWith('&gt; ')) { quote.push(line.slice(5)); continue; }
      flush();
      if (/^[-*] /.test(line)) html += '<div class="wa-li">•&nbsp;' + inline(line.slice(2)) + '</div>';
      else if (/^• /.test(line)) html += '<div class="wa-li">' + inline(line) + '</div>';
      else html += (line ? '<div>' + inline(line) + '</div>' : '<div class="wa-gap"></div>');
    }
    flush();
    return html;
  }
  window.jeliBubble = function (m, me) {
    const at = m.at ? new Date(m.at) : new Date(), clock = pad(at.getHours()) + ':' + pad(at.getMinutes());
    const div = document.createElement('div');
    if (m.role === 'system') { div.className = 'wa-system'; div.textContent = m.text; return div; }
    const mine = m.role === 'member';
    div.className = 'wa-msg ' + (mine ? 'out' : 'in');
    let html = mine ? '' : '<div class="wa-sender">Jeli</div>';
    if (m.quoted) html += '<div class="wa-reply"><b>' + esc(m.quoted[0] === 'You' ? 'You' : m.quoted[0]) + '</b><span>' + esc(String(m.quoted[1]).slice(0, 180)) + '</span></div>';
    const called = mine && m.called ? '<span class="wa-mention">@Jeli</span> ' : '';
    html += '<div class="wa-text">' + (called ? '<div>' + called + '</div>' : '') + format(m.text) + '</div>';
    html += '<div class="wa-meta">' + (m.seconds ? m.seconds + ' s · ' : '') + clock + (mine ? ' <span class="wa-ticks">✓✓</span>' : '') + '</div>';
    if (m.note) html += '<div class="wa-note">' + esc(m.note) + '</div>';
    div.innerHTML = html;
    return div;
  };
})();
"""
