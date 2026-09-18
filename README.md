# Jeli — the group memory bot

> In West Africa, the *jeli* (griot) is the keeper of the community's memory.
> Jeli does the same for a WhatsApp group: it remembers what was said in the chats and calls, and answers members directly — with sources.

Built for the **UniPods METI AI Innovation Programme — Cohort 1 Chatbot Hackathon** (18–24 September 2026).

**The problem:** the group has grown large. Messages get missed, the same questions get re-asked, and nobody rewatches call recordings.
**What Jeli does:** Jeli is a member of the WhatsApp group. It follows the conversation and the call recordings, and when someone asks — *"@Jeli what was decided about the bootcamp dates?"*, *"/catchup since Monday"* — it answers right there, with its sources. If it doesn't know, it says so.

📄 Challenge: [`Docs/UniPods Hackathon Guidlines.pdf`](Docs/UniPods%20Hackathon%20Guidlines.pdf) · Technical spec: [`Docs/hackathon_spec.md`](Docs/hackathon_spec.md)

---

## Status

| | Feature | State |
|---|---|---|
| — | FastAPI app + WhatsApp gateway (WAHA), echo replies | ✅ Day 1 |
| R1 | Chat ingestion (live group messages + WhatsApp export for the history) | ⏳ |
| R2 | Call ingestion (transcription) | ⏳ |
| R3 | Knowledge base (pgvector) | ⏳ |
| R4 | Grounded answers with sources | ⏳ |
| R5 | Replies in the group (mention, reply, name, `/command`) and in DM | ✅ |
| R6 | "I don't know" behaviour | ⏳ |
| R7–R10 | Duplicate detection, `/catchup`, meeting recaps, daily digest | ⏳ |

---

## How it works

```
WhatsApp group ──► WAHA gateway ──► webhook ──┐          ┌── Call recordings → transcription
 (Jeli's number)   (self-hosted)              ▼          ▼
                             Ingestion: normalise → chunk (author, date, source)
                                              ▼
                             Knowledge base: Postgres + pgvector (Supabase)
                                              ▼
                             Retrieval → Gemini answer, grounded and cited
                                              ▼
WhatsApp group ◄── WAHA gateway ◄──────── reply
```

### Why an unofficial gateway

The challenge asks for a bot that follows the group and **responds to people directly**. The official WhatsApp Cloud API cannot do that: it only handles one-to-one business messaging and cannot read or post in groups.

Jeli therefore connects through [**WAHA**](https://waha.devlike.pro) (open source, Apache 2.0, self-hosted), which links a WhatsApp number the way WhatsApp Web does. This is not an official Meta API, so:
- Jeli uses a **dedicated number** — never a member's personal number. If WhatsApp restricts it, nobody loses their account.
- Jeli only speaks when addressed; it never messages people unsolicited (see [Keeping Jeli's number safe](#keeping-jelis-number-safe)).
- The chat adapter is a thin, swappable layer: moving to another gateway or channel changes one module, not the core. A Telegram adapter is included as a fallback.

### When does Jeli answer?

In the group, Jeli reads everything but only replies when a message:
- **mentions** it (`@Jeli …`),
- **replies** to one of its messages,
- **starts with its name** (`Jeli, …`),
- or is a **command** (`/catchup`, `/search …`).

In a direct message, it answers everything.

---

## Keeping Jeli's number safe

WhatsApp restricts numbers that behave like machines or get reported as spam. Jeli follows [WAHA's guidance](https://waha.devlike.pro/docs/overview/how-to-avoid-blocking/).

**Built into the code** ([`app/adapters/pacing.py`](app/adapters/pacing.py), [`whatsapp_waha.py`](app/adapters/whatsapp_waha.py)):

| Guard | Behaviour |
|---|---|
| Never starts a conversation | Jeli only replies to messages addressed to it |
| Human rhythm | Short pause, *seen*, *typing…* for a time that grows with the answer's length, then the reply |
| No bursts | At least `WHATSAPP_MIN_SEND_INTERVAL_SECONDS` (3 s, randomised) between two messages sent |
| Per-member limit | At most `WHATSAPP_USER_LIMIT` answers per member per `WHATSAPP_USER_WINDOW_SECONDS` (5 per 10 min) |
| Hourly ceiling | At most `WHATSAPP_HOURLY_LIMIT` answers per hour in total (120): a bot-to-bot loop stops there |
| No backlog bursts | Messages older than 10 minutes (delivered after a reconnection) are not answered |
| Pauses on trouble | Silent while the WAHA session is not `WORKING` |
| Ordinary device | Appears as *Google Chrome (Windows)* in Linked devices; no link previews |
| Restriction alerts | WhatsApp errors 463/475 are logged with the right reaction: wait, do not re-link |

**Operating rules for the team:**
1. **Warm the number up before linking it** (24–48 h): profile photo, name *Jeli*, an *About* line saying it is the cohort's AI assistant. Team members save the number as a contact and exchange a few normal messages with it from the phone.
2. **Get Jeli introduced.** An organiser adds the number to the group and announces it, asking members to save the contact. Numbers that are saved and talked to are not flagged as spam; being reported by a few people is what gets a number banned.
3. **Link once, by QR code**, on one WAHA instance, on a stable host. Do not re-scan repeatedly or run two instances of the same session.
4. **Keep the phone alive**: charged and online at least every few days, or WhatsApp logs linked devices out (about 14 days).
5. **Use the number only for Jeli**: no manual mass messages, no joining other groups, no `@all` mentions.
6. **If WhatsApp restricts the number** (errors 463/475 in the logs): do nothing, it lifts on its own. Restarting or re-linking makes it worse.
7. **Keep a plan B**: a second SIM warmed up the same way, and the Telegram adapter.

---

## Setup

### Prerequisites
- Python 3.11+
- Docker (to run WAHA locally) — or deploy WAHA directly on Railway (see [Deploy](#deploy-railway))
- A **dedicated phone number** with WhatsApp installed, for Jeli

### 1. Install
```bash
git clone https://github.com/professeurp2/jeli.git
cd jeli
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```
In `.env`, set `WAHA_API_KEY`, `WAHA_WEBHOOK_HMAC_KEY` and `WAHA_DASHBOARD_PASSWORD` to random strings:
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Start WAHA and link Jeli's number
```bash
docker compose up -d
```
1. Open <http://localhost:3000/dashboard> (user `admin`, password `WAHA_DASHBOARD_PASSWORD`). The dashboard asks for the API key: use `WAHA_API_KEY`.
2. Session `default` shows a QR code. On Jeli's phone: **WhatsApp → Linked devices → Link a device**, and scan it.
3. The session turns **WORKING**. The link survives restarts (stored in `data/waha/`).

### 3. Start Jeli
```bash
uvicorn app.main:app --reload
```
<http://127.0.0.1:8000/health> must show `"whatsapp": true`.

Send Jeli's number a direct message, or add it to a group and mention it: it replies.

### 4. Restrict Jeli to the cohort group
Jeli logs the id of each group that talks to it (`…@g.us`). Put the cohort group's id in `WHATSAPP_GROUP_IDS` so Jeli ignores any other group its number is added to.

### Tests
```bash
pytest
```

---

## Deploy (Railway)

Two services in one Railway project:

**1. `waha`** — *New → Docker Image* → `devlikeapro/waha:gows`
- Add a **volume** mounted at `/app/.sessions` (keeps the WhatsApp link across redeploys).
- Variables:
  ```
  WHATSAPP_DEFAULT_ENGINE=GOWS
  WHATSAPP_START_SESSION=default
  WAHA_CLIENT_BROWSER_NAME=Chrome
  WAHA_CLIENT_DEVICE_NAME=Windows
  WAHA_SESSION_CONFIG_IGNORE_STATUS=true
  WAHA_SESSION_CONFIG_IGNORE_CHANNELS=true
  WAHA_SESSION_CONFIG_IGNORE_BROADCAST=true
  WAHA_API_KEY=<same as Jeli>
  WAHA_DASHBOARD_USERNAME=admin
  WAHA_DASHBOARD_PASSWORD=<strong password>
  WHATSAPP_SWAGGER_USERNAME=admin
  WHATSAPP_SWAGGER_PASSWORD=<strong password>
  WHATSAPP_HOOK_URL=http://jeli.railway.internal:8000/waha/webhook
  WHATSAPP_HOOK_EVENTS=message,session.status
  WHATSAPP_HOOK_HMAC_KEY=<same as Jeli's WAHA_WEBHOOK_HMAC_KEY>
  ```
- *Settings → Networking → Generate domain* (target port `3000`) to reach the dashboard and scan the QR code.

**2. `jeli`** — *New → GitHub repo* → this repository
- Variables:
  ```
  PORT=8000
  WAHA_URL=http://waha.railway.internal:3000
  WAHA_API_KEY=<same as WAHA>
  WAHA_WEBHOOK_HMAC_KEY=<same as WAHA's WHATSAPP_HOOK_HMAC_KEY>
  WHATSAPP_GROUP_IDS=<cohort group id>
  ```
- The start command comes from the `Procfile`. The two services talk over Railway's private network; Jeli needs no public domain.

Then link the number from the WAHA dashboard, as in step 2 above.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WAHA_URL` | WhatsApp | Where Jeli reaches WAHA, e.g. `http://localhost:3000` |
| `WAHA_API_KEY` | with `WAHA_URL` | WAHA API key (`X-Api-Key`) |
| `WAHA_WEBHOOK_HMAC_KEY` | with `WAHA_URL` | Verifies the HMAC-SHA512 signature of every webhook call; forged calls are rejected |
| `WAHA_SESSION` | no | WAHA session name, default `default` |
| `WHATSAPP_GROUP_IDS` | recommended | Comma-separated group ids Jeli may listen to; empty = all its groups |
| `BOT_NAME` | no | A message starting with this name is addressed to Jeli. Default `Jeli` |
| `WHATSAPP_USER_LIMIT` / `WHATSAPP_USER_WINDOW_SECONDS` | no | Answers per member per window. Default 5 per 600 s |
| `WHATSAPP_HOURLY_LIMIT` | no | Answers per hour, all chats together. Default 120 |
| `WHATSAPP_MIN_SEND_INTERVAL_SECONDS` | no | Minimum gap between two messages sent. Default 3 |
| `GEMINI_API_KEY` | from Day 3 | Google AI Studio key (LLM + embeddings) |
| `DATABASE_URL` | from Day 2 | Supabase Postgres connection string |
| `LOG_LEVEL` | no | Default `INFO` |

<details>
<summary>Telegram (fallback channel)</summary>

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather). Also run `/setprivacy` → **Disable** so the bot can read the group |
| `PUBLIC_URL` | Public HTTPS URL of the app. Set → webhook mode; empty → polling mode |
| `TELEGRAM_WEBHOOK_SECRET` | Required with `PUBLIC_URL`; checked on every Telegram webhook call |
</details>

---

## Project layout

```
app/
├── main.py            # FastAPI app, lifecycle, /health
├── config.py          # settings from environment / .env
├── models.py          # platform-independent message types
├── adapters/          # whatsapp_waha.py, telegram.py — thin, swappable
├── answer/            # responder.py → RAG, prompts, citations
├── ingest/            # chat export parser, transcription, chunking
├── kb/                # embeddings, pgvector store, search
└── jobs/              # daily digest, duplicate check
tests/
docker-compose.yml     # local WAHA gateway
Docs/                  # challenge guidelines and technical spec
```

---

## Privacy

- Jeli uses its own dedicated number and only listens to the groups listed in `WHATSAPP_GROUP_IDS`.
- Group members should be told that Jeli is in the group and what it remembers.
- Every webhook call from WAHA is signed (HMAC-SHA512) and verified; the WAHA API and dashboard are protected by a key and a password.
- Chat exports, recordings and the WhatsApp session stay out of the repository (`data/` is git-ignored).
- What is stored, and how to delete it, will be documented here as ingestion lands.
