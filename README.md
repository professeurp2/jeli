# Jeli — the group memory bot

> In West Africa, the *jeli* (griot) is the keeper of the community's memory.
> Jeli does the same for a WhatsApp group: it remembers what was said in the chats and calls, and answers members directly — with sources.

Built for the **UniPods METI AI Innovation Programme — Cohort 1 Chatbot Hackathon** (18–24 September 2026).

**The problem:** the group has grown large. Messages get missed, the same questions get re-asked, and nobody rewatches call recordings.
**What Jeli does:** it ingests the group's messages and call recordings, then answers questions like *"What was decided about the bootcamp dates?"* or *"What did I miss this week?"* on WhatsApp, with a grounded answer and its source. If it doesn't know, it says so.

📄 Reference: [`Docs/hackathon_brief.md`](Docs/hackathon_brief.md) (supersedes the channel choice in [`Docs/hackathon_spec.md`](Docs/hackathon_spec.md))

---

## Status

| | Feature | State |
|---|---|---|
| — | FastAPI app, WhatsApp Cloud API webhook (echo) | ✅ Day 1 |
| R1 | Chat ingestion (WhatsApp export + live messages) | ⏳ |
| R2 | Call ingestion (transcription) | ⏳ |
| R3 | Knowledge base (pgvector) | ⏳ |
| R4 | Grounded answers with sources | ⏳ |
| R5 | Replies on WhatsApp (DM) | ✅ |
| R6 | "I don't know" behaviour | ⏳ |
| R7–R10 | Duplicate detection, `/catchup`, meeting recaps, daily digest | ⏳ |

---

## How it works — and the WhatsApp constraint

**The official WhatsApp Cloud API cannot read group conversations** — it only handles direct messages. So Jeli separates *ingesting* from *answering*:

| | Path | Official? |
|---|---|---|
| **Ingest** the history | WhatsApp "Export chat" `.txt` → parser | ✅ |
| **Answer** members | Direct message to Jeli via the Cloud API | ✅ |
| Live group reading / replies (optional) | Baileys bridge on a dedicated number | ⚠️ unofficial |

The fully official path already covers every requirement. The unofficial bridge is an extra: if it breaks, Jeli keeps working.

```
WhatsApp group ─┐                   ┌─ Call recordings → transcription
 (export .txt)  ▼                   ▼
        Ingestion: normalise → chunk (author, date, source, link)
                           ▼
        Knowledge base: Postgres + pgvector (Supabase)
                           ▼
        Retrieval → Gemini answer, grounded and cited
                           ▼
        Adapter: WhatsApp Cloud API (DM) · Telegram (fallback)
```

Adapters are thin and swappable: the core never depends on a chat platform. Each adapter is enabled as soon as its environment variables are set.

---

## Setup

### Prerequisites
- Python 3.11+
- A Meta developer account ([developers.facebook.com](https://developers.facebook.com))

### 1. Install
```bash
git clone https://github.com/professeurp2/jeli.git
cd jeli
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

### 2. Create the WhatsApp app (Meta)
1. **My Apps → Create app** → use case *Connect with customers through WhatsApp* (type **Business**).
2. **WhatsApp → API Setup**: Meta provides a free **test number**. Copy its **Phone number ID** → `WHATSAPP_PHONE_NUMBER_ID`.
3. Still in API Setup, **add the recipient numbers** allowed to talk to the bot. ⚠️ A test number only works with **up to 5 registered numbers** — register the judges' numbers in advance.
4. **App settings → Basic → App secret** → `WHATSAPP_APP_SECRET`.
5. **Permanent access token** (the one shown in API Setup expires after 24 h):
   [Business settings](https://business.facebook.com/settings) → **Users → System users** → add an admin system user → **Assign assets** (the app and the WhatsApp account) → **Generate token** with `whatsapp_business_messaging` and `whatsapp_business_management`, expiry *Never* → `WHATSAPP_ACCESS_TOKEN`.
6. Choose any random string → `WHATSAPP_VERIFY_TOKEN`:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`

### 3. Run
```bash
uvicorn app.main:app --reload
```
Health check: <http://127.0.0.1:8000/health> shows which channels are enabled.

Meta can only call a **public HTTPS** URL. To test locally, expose the app with a tunnel, e.g. `cloudflared tunnel --url http://localhost:8000`, and use that URL in the next step.

### 4. Connect the webhook
**WhatsApp → Configuration → Webhook → Edit**:
- Callback URL: `https://<your-domain>/whatsapp/webhook`
- Verify token: the value of `WHATSAPP_VERIFY_TOKEN`
- **Verify and save**, then subscribe to the **`messages`** field.

Send a WhatsApp message to the test number from a registered phone: Jeli replies.

### Tests
```bash
pytest
```

---

## Deploy (Railway)

1. On [railway.com](https://railway.com): **New project → Deploy from GitHub repo** → select this repository.
2. Add the environment variables (see below).
3. **Settings → Networking → Generate domain** — this is the public URL for the Meta webhook.
4. Set the webhook in Meta as in step 4 above.

The start command comes from the `Procfile`: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `WHATSAPP_ACCESS_TOKEN` | WhatsApp | Permanent system-user token |
| `WHATSAPP_PHONE_NUMBER_ID` | WhatsApp | Phone number ID from API Setup |
| `WHATSAPP_APP_SECRET` | WhatsApp | Checks the `X-Hub-Signature-256` of every webhook call |
| `WHATSAPP_VERIFY_TOKEN` | WhatsApp | Shared secret for the webhook verification handshake |
| `WHATSAPP_API_VERSION` | no | Graph API version, default `v25.0` |
| `GEMINI_API_KEY` | from Day 3 | Google AI Studio key (LLM + embeddings) |
| `DATABASE_URL` | from Day 2 | Supabase Postgres connection string |
| `LOG_LEVEL` | no | Default `INFO` |

<details>
<summary>Telegram (fallback channel)</summary>

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token from [@BotFather](https://t.me/BotFather). In BotFather, also run `/setprivacy` → **Disable** so the bot can read the group |
| `PUBLIC_URL` | Public HTTPS URL of the app. Set → webhook mode; empty → polling mode (no public URL needed locally) |
| `TELEGRAM_WEBHOOK_SECRET` | Required with `PUBLIC_URL`; random string checked on every Telegram webhook call |

In a Telegram group, Jeli answers when mentioned (`@your_bot …`) or replied to; in DM it answers everything.
</details>

---

## Project layout

```
app/
├── main.py            # FastAPI app, lifecycle, /health
├── config.py          # settings from environment / .env
├── models.py          # platform-independent message types
├── adapters/          # whatsapp_cloud.py, telegram.py — thin, swappable
├── answer/            # responder.py → RAG, prompts, citations
├── ingest/            # chat export parser, transcription, chunking
├── kb/                # embeddings, pgvector store, search
└── jobs/              # daily digest, duplicate check
tests/
Docs/                  # hackathon brief, spec and guidelines
```

---

## Privacy

- Jeli only ingests the group it is authorised to use.
- Every WhatsApp webhook call is checked against the app secret; forged calls are rejected.
- Chat exports and recordings stay out of the repository (`data/` is git-ignored).
- What is stored, and how to delete it, will be documented here as ingestion lands.
