# Jeli — the group memory bot

> In West Africa, the *jeli* (griot) is the keeper of the community's memory.
> Jeli does the same for a chat group: it remembers what was said in the chats and calls, and answers members directly — with sources.

Built for the **UniPods METI AI Innovation Programme — Cohort 1 Chatbot Hackathon** (18–24 September 2026).

**The problem:** the group has grown large. Messages get missed, the same questions get re-asked, and nobody rewatches call recordings.
**What Jeli does:** it ingests the group's messages and call recordings, then answers questions like *"What was decided about the bootcamp dates?"* or *"What did I miss this week?"* with a grounded answer and a link to the source. If it doesn't know, it says so.

📄 Full specification: [`Docs/hackathon_spec.md`](Docs/hackathon_spec.md)

---

## Status

| | Feature | State |
|---|---|---|
| — | FastAPI app + Telegram bot (echo) | ✅ Day 1 |
| R1 | Chat ingestion (WhatsApp export + live messages) | ⏳ |
| R2 | Call ingestion (transcription) | ⏳ |
| R3 | Knowledge base (pgvector) | ⏳ |
| R4 | Grounded answers with sources | ⏳ |
| R5 | Replies in the group (mention) and in DM | ✅ |
| R6 | "I don't know" behaviour | ⏳ |
| R7–R10 | Duplicate detection, `/catchup`, meeting recaps, daily digest | ⏳ |

---

## How it works

```
Group chat ─┐                 ┌─ Call recordings → transcription
            ▼                 ▼
        Ingestion: normalise → chunk (author, date, source, link)
                        ▼
        Knowledge base: Postgres + pgvector (Supabase)
                        ▼
        Retrieval → Gemini answer, grounded and cited
                        ▼
        Bot adapter (Telegram today, WhatsApp optional)
```

The chat adapter is a thin, swappable layer: the core never depends on a chat platform.

---

## Run it locally

### Prerequisites
- Python 3.11+
- A Telegram bot token (see below)

### 1. Create the Telegram bot
1. In Telegram, open [@BotFather](https://t.me/BotFather) → `/newbot` → pick a name and a username ending in `bot`. Copy the token.
2. Still in BotFather: `/setprivacy` → select your bot → **Disable**. Without this, the bot only sees commands and mentions in groups, and cannot build the group's memory.
3. Add the bot to your group.

### 2. Install and configure
```bash
git clone https://github.com/professeurp2/jeli.git
cd jeli
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # then set TELEGRAM_BOT_TOKEN in .env
```

### 3. Start
```bash
uvicorn app.main:app --reload
```
With `PUBLIC_URL` empty, the bot runs in **polling mode**: no public URL needed. Mention it in the group (`@your_bot hello`) or send it a direct message.

Health check: <http://127.0.0.1:8000/health>

### Tests
```bash
pytest
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Token from @BotFather |
| `PUBLIC_URL` | in production | Public HTTPS URL of the app. Set → webhook mode; empty → polling mode |
| `TELEGRAM_WEBHOOK_SECRET` | with `PUBLIC_URL` | Random string checked on every webhook call. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `GEMINI_API_KEY` | from Day 3 | Google AI Studio key (LLM + embeddings) |
| `DATABASE_URL` | from Day 2 | Supabase Postgres connection string |
| `LOG_LEVEL` | no | Default `INFO` |

---

## Deploy (Railway)

1. On [railway.com](https://railway.com): **New project → Deploy from GitHub repo** → select this repository.
2. Set the variables above (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, …).
3. **Settings → Networking → Generate domain**, then set `PUBLIC_URL` to that domain (e.g. `https://jeli-production.up.railway.app`).
4. Redeploy. On startup the app registers its Telegram webhook automatically.

The start command comes from the `Procfile`: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

> Only one instance can receive updates: when the deployed bot is in webhook mode, a local copy in polling mode will switch Telegram back to polling. Use a separate test bot for local development.

---

## Project layout

```
app/
├── main.py         # FastAPI app, lifecycle, /health
├── config.py       # settings from environment / .env
├── models.py       # platform-independent message types
├── adapters/       # telegram.py (WhatsApp later) — thin, swappable
├── answer/         # responder.py → RAG, prompts, citations
├── ingest/         # chat export parser, transcription, chunking
├── kb/             # embeddings, pgvector store, search
└── jobs/           # daily digest, duplicate check
tests/
Docs/               # hackathon spec and guidelines
```

---

## Privacy

- Jeli only ingests the group it has been added to by its members.
- Chat exports and recordings stay out of the repository (`data/` is git-ignored).
- What is stored, and how to delete it, will be documented here as ingestion lands.
