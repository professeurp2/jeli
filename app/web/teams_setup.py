"""The page an IT administrator reads before deciding, at a public address anyone can send them.

Jeli can take a Microsoft Teams meeting into the community's memory in two ways. One needs nobody's
permission: someone downloads the meeting's transcript and shares it, and Jeli reads it like any
document (app/answer/documents.py). The other is automatic — Jeli fetches the transcripts itself —
and it needs a Microsoft 365 administrator of the tenant that owns the meetings to grant it.

That permission is wide: it reads meeting transcripts across the organisation. An administrator is
right to want it in writing before granting it, and wrong to grant it from a chat message. So this
is the writing: what Jeli would read, what it would not, where it goes, how long it stays, and the
two exact commands. It is open to anyone with the link — an administrator has no Jeli account, and
should not need one to read a request addressed to them.

In English on purpose: it is read by the people who run the tenant, and this is the language their
documentation and their consent screens are in.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.web import ui
from app.web.ui import esc

router = APIRouter()

# The one permission asked for, and the one command that scopes it. Both are quoted from Microsoft's
# own documentation so an administrator can check them against it rather than trust this page.
PERMISSION = "OnlineMeetingTranscript.Read.All"
POLICY_COMMANDS = """\
# 1. Create a policy naming the application that may read transcripts
New-CsApplicationAccessPolicy -Identity Jeli-Transcripts \\
  -AppIds "<APPLICATION-ID>" -Description "Jeli reads meeting transcripts"

# 2. Grant it — to one organiser, or to everyone whose meetings Jeli should read
Grant-CsApplicationAccessPolicy -PolicyName Jeli-Transcripts -Identity "diane@example.org"
"""

READS = [
    ("The text of meeting transcripts", "Only meetings whose organiser is covered by the policy below, and only when someone turned transcription on during the meeting."),
    ("Who said what, and when", "The speaker's name and the time, as Teams itself records them."),
]
DOES_NOT_READ = [
    ("No audio, no video", "Jeli never receives the recording itself, and never joins a meeting."),
    ("No mailboxes, no files, no chats", "The permission covers meeting transcripts and nothing else."),
    ("No meeting it is not scoped to", "The access policy below names exactly whose meetings it may read. Outside it, Microsoft refuses the call."),
]
KEEPING = [
    ("Where it is stored", "A private Postgres database (Supabase, EU region), reachable only by this application."),
    ("What it is used for", "Answering members' questions in the community's WhatsApp groups, always citing who said it."),
    ("Who can read it back", "Members of the community, through Jeli, and the five people on its team through the dashboard."),
    ("Removing it", "Revoking the policy stops it at once; the team can delete any transcript from the dashboard."),
]


def _rows(items: list[tuple[str, str]]) -> str:
    return "".join(
        f'<div class="row"><div class="row-text"><b>{esc(title)}</b><span>{esc(text)}</span></div></div>'
        for title, text in items
    )


@router.get("/teams", response_class=HTMLResponse)
async def teams_setup(request: Request) -> HTMLResponse:
    settings = get_settings()
    who = settings.railway_public_domain or "this deployment"
    body = (
        f'<div class="login-brand"><span class="login-avatar">{ui.LEMUR}</span><h1>Jeli</h1>'
        "<p>Reading Microsoft Teams meeting transcripts — a request for your IT administrator</p></div>"
        + ui.notice(
            "info",
            "Nothing here is needed to try Jeli. Anyone can already download a meeting's transcript "
            "and share it, and Jeli will read it. This page is about doing it automatically.",
        )
        + ui.card(
            "What is being asked for",
            f'<div class="rows">'
            + _rows([
                ("One Microsoft Graph application permission", PERMISSION),
                ("One Teams application access policy", "It names whose meetings the application may read. Without it, Microsoft refuses every call, even with the permission granted."),
                ("Admin consent", "The permission ends in .All, so a Global Administrator must grant it. A user cannot grant it to themselves — by design."),
            ])
            + "</div>",
            icon_name="key",
            description="Two steps, both in the tenant that owns the meetings.",
        )
        + ui.card("What Jeli would read", f'<div class="rows">{_rows(READS)}</div>', icon_name="book")
        + ui.card("What it would never read", f'<div class="rows">{_rows(DOES_NOT_READ)}</div>', icon_name="shield")
        + ui.card("Where it goes, and for how long", f'<div class="rows">{_rows(KEEPING)}</div>', icon_name="list")
        + ui.card(
            "The two commands",
            "<p class='hint'>Run in Teams PowerShell, after the application is registered in Microsoft Entra ID "
            "and the permission above is granted. Replace the application id and the organiser.</p>"
            f"<pre class='quote' style='white-space:pre-wrap'>{esc(POLICY_COMMANDS)}</pre>"
            "<p class='hint'>Microsoft's own documentation: “To use application permission for this API, tenant "
            "administrators must create an application access policy and grant it to a user.”</p>",
            icon_name="code" if "code" in ui.ICONS else "list",
        )
        + ui.card(
            "If the answer is no",
            "<p class='hint'>That is a reasonable answer, and Jeli works without it. Someone downloads the "
            "transcript from the meeting and shares it in the group — Jeli reads it the same way, and nobody "
            "has to grant anything. The only difference is that a person has to remember.</p>",
            icon_name="info",
        )
        + f'<p class="login-foot">Asked by the Jeli team for {esc(who)}. '
        "Questions about what is stored, or a request to remove it, go to the team on WhatsApp.</p>"
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Jeli and Microsoft Teams</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{ui.CSS}</style></head>
<body class="login-body"><main class="public">{body}</main></body></html>"""
    )
