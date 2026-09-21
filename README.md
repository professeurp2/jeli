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
| — | FastAPI app + WhatsApp gateway (WAHA), echo replies, CI/CD | ✅ Day 1 |
| R1 | Chat ingestion: WhatsApp export import + live group messages | ✅ Day 2 |
| R2 | Call ingestion: YouTube, audio/video files, Teams/Zoom transcripts; answers link to the moment | ✅ Day 4 |
| R3 | Knowledge base: conversation chunks, Gemini embeddings, hybrid search (pgvector + keywords) | ✅ Day 2 |
| R4 | Grounded answers with sources (Gemini, with model fallback) | ✅ Day 3 |
| R5 | Replies in the group (mention, reply, name, `/command`) and in DM | ✅ |
| R6 | "I don't know" behaviour | ✅ Day 3 |
| R12 | Answers in the language of the question (French / English) | ✅ Day 3 |
| R7 | Duplicate detection: a question the group already answered gets a pointer to that answer | ✅ Day 5 |
| R8 | Catch-up digest: `/catchup`, "what did I miss since Monday?" | ✅ Day 5 |
| R9 | Session recaps: summary, decisions, action items with owners, key moments linked to the video | ✅ Day 6 |
| R10 | Daily digest in each group (opt-in) | ✅ Day 6 |
| R11 | `/search <topic>`: where the group talked about it, without a model call | ✅ Day 6 |
| R13 | The team's control panel: pause, try, questions, deadlines, old conversations, activities, misuse watchlist, exceptions, settings, WhatsApp link, activity log | ✅ |
| R14 | Deadline reminders: `/deadlines`, and "coming up" in every digest | ✅ |

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

**It follows the conversation:** after Jeli answers a member, their next messages in that chat are for Jeli for a few minutes (Settings → "Follow the conversation") when they read like a follow-up — *"and what do we submit?"*, *"send me the guide"* — without repeating "Jeli"; not when they reply to or mention someone else, and "thanks" closes the conversation. A follow-up is rewritten with the conversation before Jeli searches (*"and what do we submit?"* → *"what do we submit for the hackathon?"*).

**Sources, the WhatsApp way:** WhatsApp references a message by replying to it. When the source was said in the same chat (and Jeli received it live), Jeli **replies to that message** — WhatsApp quotes it above the answer and a tap jumps to it — and @mentions the member who asked. Otherwise (history imported from an export, another group, a session, a document) it **quotes** it with WhatsApp's quote block. Illustrative output (name made up):
```
Submissions close on Thursday 24 September: a working chatbot, the code link and setup notes.

> *Awa T.* · METI cohort, Thu 17 Sep
> Build Phase: Friday 18 Sept to Thursday 24 Sept…
```

**One door for every message.** Greetings, thanks and slash commands are answered at once; every other message goes through one understanding step ([`app/answer/understand.py`](app/answer/understand.py)): a model reads it with the conversation so far, the community brief and what Jeli can do, and says what the member wants — a question, a catch-up (and since when), a session recap or a question about one session, the deadlines, a document, a listing of what Jeli has, the previous answer's sources ("source?"), the previous answer by voice ("en vocal"), an image, small talk, a question about Jeli itself, or an ambiguous request that deserves one short question back with two or three options. It also gives the language the member writes in and the search queries (English and the member's language). The search on the raw message starts in parallel, so the two model calls cost the time of one. Measured before this change (21 Sep, production): routing was a cascade of regular expressions, and "recap d'aujourd'hui", "session 4", "en vocal", "Thank you Jeli", "how can you help a visually impaired person?" all went wrong; the expressions remain as the fallback when no model answers. When the model finds no answer but the group discussed something close, Jeli shows the closest discussion instead of a flat "I don't know".

**One voice.** Every prompt starts from the same persona ([`app/answer/persona.py`](app/answer/persona.py)): the member's language and register, short answers, WhatsApp formatting, no closing lines, never "I am the group's memory".

**What Jeli keeps in mind.** Three memories, all in the database so that a deployment loses nothing: the **conversation** with each member (the last turns, 30 minutes, with the sources of each answer — private ones a day at most, never indexed); what it knows of the **member** (name, the language they write in, their own introduction, their last topic — `/oublie-moi` is not needed: the team can clear it from the dashboard); and the **community brief** ([`app/answer/brief.py`](app/answer/brief.py)): every few hours a model rewrites, from the documents, the organisers' announcements and the session recaps, a short brief of the programmes, organisers, rules, dates and sessions. The brief is background for every prompt — Jeli knows what Wadhwani Ignite is or who Diane is without searching — never a source to quote.

**Sources, verified.** The model cites the exact message lines that state its answer; a cited line that shares no word, number or date with the answer is dropped (measured on 21 Sep: attribution by word overlap on whole chunks produced "random" references). One verified source is then shown under factual answers — never under small talk or a catch-up — and the rest is kept for "source?". Settings → *Show where an answer comes from*: one source, on request, or never.

**Documents and files:** Jeli keeps the documents members share in the groups (PDF, Word, text) and those the team adds on the dashboard. It learns them page by page — answers quote *"📄 Hackathon guidelines, page 1"* — and **sends the file** when a member asks for it (*"send me the guidelines"*), as a WhatsApp document replying to their message. Asked for it in another language (*"envoie-moi le guide en français"*), it answers at once that it is translating, then sends a translated PDF a minute later (machine translation, marked as such; English, French, Portuguese, Spanish, Swahili, German, Italian, Dutch), and keeps it for the next request. **Past documents:** chat histories exported *without media* only say "<document omitted>". Export them *with media* and import them on the dashboard: Jeli keeps each PDF, Word or text file with who shared it and when. The Knowledge page also lists the documents mentioned in the groups whose file is missing, each with a button to give it to Jeli; meanwhile Jeli tells members the document exists but it doesn't have it.

**Meetings, on their own:** organisers — the groups' admins, and anyone the team lists on the dashboard — share each recording's link with a description. When one of them posts a link, a model reads the message and tells whether it shares a session's recording, which session and which day ("yesterday's Module 2 class"). A recording on YouTube, or on Google Drive shared with anyone with the link, is transcribed in the background — about ten minutes per hour of video — its recap written, and Jeli then quotes it to the minute (for YouTube, with a link that starts playing there). A recording Jeli cannot watch (Teams, SharePoint, Drive behind a sign-in) is kept as a link, so Jeli can say where it is. Recordings the organisers shared in an old conversation imported on the dashboard are found the same way. The team can also add a session from the dashboard (Knowledge → *Add a recorded session*). Jeli cannot join a live call.

**Voice notes:** a member can ask Jeli by voice — a voice note sent to Jeli, replying to one of its messages, or in a conversation with it (voice notes between members are never listened to). Jeli listens, answers **by voice** (a WhatsApp voice note replying to theirs, *"recording audio…"* shown meanwhile), then writes the sources, which a voice note cannot quote. A written question ending in *"reply by voice"* / *« réponds en vocal »* gets a voice note too. Long answers (lists, catch-ups) stay written; if the voice note cannot be made, the written answer goes instead. Try it on the dashboard's *Try Jeli* page.

**Organisers count most:** the groups' admins and the organisers the team lists (`ORGANISERS`, then the dashboard's Exceptions page — names, numbers, or both: "Diane +250 …") are marked as organisers in what Jeli reads. Their announcements rank first among the excerpts, prevail over members' claims, open the catch-ups, and are quoted by name. **Polls** posted in the groups are remembered with their options, and each vote is counted (WAHA's `poll.vote` events): *"what did people vote for the demo day?"* gets the running tally.

**Never a bare "I don't know":** when the groups hold no answer, Jeli says so and adds what it knows of the situation, without inventing anything: the recording of that session is being transcribed (the first 30 minutes are done — ask again in a few minutes), the session is scheduled for Tuesday, the recording is a Teams link it cannot watch (here it is), its memory of the groups stops on Friday, a document was shared but its file is missing — then one next step. The dashboard counts these as questions Jeli couldn't answer.

**One exception (R7):** when a member asks *the group* a question that the group already answered — a reply to someone who asked before, or an announcement that states it — Jeli points to that answer, uninvited, replying to it when it can.
It speaks up only when sure: the question must look like one, be very close to an indexed conversation (`DUPLICATE_MIN_SIMILARITY`, 0.70), and the model must confirm that an excerpt answers *this* question — same topic is not enough, and a question left unanswered stays unanswered. At most `DUPLICATE_REPLIES_PER_HOUR` (3) such replies per group; `DUPLICATE_DETECTION=false` turns it off.

**Session recaps (R9):** `/recap` lists the recorded sessions; `/recap 2`, *"summary of the coaching session"* or *"de quoi a-t-on parlé pendant la session d'accueil ?"* give that session's summary, decisions, action items (owner, due date) and key moments, each with a link to that second of the video. A recap is written once from the whole transcript (one model call) and stored per language: English at import, French on first request — then it answers instantly.

**Search (R11):** `/search <topic>` lists where the group talked about a topic, with snippets — no model call, so it keeps working when the models are out of quota.

**Daily digest (R10), opt-in:** with `DAILY_DIGEST_TIME=17:00` (UTC), each group in `WHATSAPP_GROUP_IDS` gets the last 24 hours' digest once a day — only if there is something new, never twice (each run is claimed in the database, so a restart cannot repeat it), and within the anti-ban limits.

**Deadlines (R14):** Jeli scans new messages and call transcripts every hour for deadlines a message states — resolving "Friday" or "tomorrow" from that message's date, skipping guesses, keeping an organiser's announcement over a member's contradicting claim, and merging two wordings of the same deadline. `/deadlines` (or *"what are the upcoming deadlines?"*) lists the next two weeks with who announced each and where; every catch-up and daily digest ends with what is due in the next three days. Reminders never come as extra messages of their own.

**Dashboard (R13): the team's control panel.** `https://<jeli-domain>/dashboard`, in plain words — no model names, thresholds or ids. Each team member signs in with their own password; everything they change applies at once, without a redeploy, and is written to the activity log with their name.

| Page | What the team does there |
|---|---|
| Overview | **Live**: Jeli's state, today's activity, the live feed of exchanges (groups, private, tries), what needs attention, questions per day, coming deadlines, activities. Pages update by themselves within seconds when something changes |
| **Pause Jeli** (every page) | One click: Jeli answers nobody and posts nothing, but keeps remembering the groups |
| Try Jeli | Talk to Jeli as a member would, in the group of your choice or in private, shown exactly as on WhatsApp (bubbles, quoted message, formatting). Each member's conversation is kept. Nothing reaches WhatsApp; works while paused |
| Questions | What the groups asked and what Jeli couldn't answer (7 or 30 days), copy or download — for the FAQ, the pitch, the next features |
| Deadlines | Remove a wrong one (it is never found again), add one announced elsewhere |
| Knowledge | Documents (add, download, remove), recorded sessions (add from a YouTube or Google Drive link, follow the transcription, remove), old conversations from WhatsApp's "Export chat" (.txt or .zip, checked before adding), how conversations are cited |
| Activities | Switch on/off, run now or stop: memory updates, deadline finding, the daily summary (time, language), the weekly team report (day, time) |
| Watchlist | Members who misuse Jeli (see below): block, unblock, forgive |
| Exceptions | People Jeli never quotes (other bots), people it doesn't answer, the groups it works in |
| Settings | How sure Jeli must be before answering (careful / balanced / relaxed), its name, pointing to earlier answers, pace limits |
| WhatsApp | Connection state, the code to scan to link Jeli's phone, how to keep the number safe |
| Team | Members, change one's own password, the activity log |

Settings changed on the dashboard are kept in `jeli.settings` and win over the environment; secrets and the team's phone numbers are never among them. Signing in sets a signed session cookie (`DASHBOARD_SECRET`); every change carries a token tied to the session and is refused from another site; five wrong passwords in 15 minutes and that name or address must wait. Accounts: `python -m scripts.dashboard_users --passwords <file> <names…>` writes new passwords to a file to hand out and prints `DASHBOARD_USERS` (salted scrypt hashes only); members can then choose their own. No accounts = no dashboard.

**Misuse (watchlist):** Jeli spots members who try to wear it out or turn it against its rules — floods of questions (past the per-member limit), the same message three times in 10 minutes, oversized messages, attempts to make it ignore its instructions — and records each incident (who and what, never the message). Three within an hour and Jeli stays silent with that member for an hour on its own; the team can block them for good.

No member appears in usage figures: usage is counted as anonymous events, group questions are kept without their author, mentions or phone numbers, and private questions are never kept. 

**Weekly team report:** once a week (`TEAM_REPORT_TIME`, e.g. `mon 07:00` UTC), each team member (`TEAM_NUMBERS`) gets the dashboard's news in a private message: questions and outcomes, what Jeli could not answer, the latest group questions, what is due in the next 7 days, and a link to the dashboard. Private messages a number starts are what WhatsApp watches most, so: only numbers that are on WhatsApp (checked first), a minute or so apart, never twice in a week (claimed in the database, by a hash of the number), and nothing in a quiet week. **Each member saves Jeli's number and sends it a first message** before the first report, so it is a conversation they started. The numbers are personal data: set them on the server only, never in the repository.

**Catch-up (R8):** `/catchup`, `/catchup 3 days`, *"@Jeli what did I miss since Monday?"*, *"Jeli, qu'est-ce que j'ai raté cette semaine ?"* → highlights, decisions, deadlines and dates, questions still unanswered, and the sessions recorded in that period with their links. Default period: the last 24 hours. The same digest is reused for 10 minutes, so a whole jury asking at once costs one model call.

---

## Keeping Jeli's number safe

WhatsApp restricts numbers that behave like machines or get reported as spam. Jeli follows [WAHA's guidance](https://waha.devlike.pro/docs/overview/how-to-avoid-blocking/).

**Built into the code** ([`app/adapters/pacing.py`](app/adapters/pacing.py), [`whatsapp_waha.py`](app/adapters/whatsapp_waha.py)):

| Guard | Behaviour |
|---|---|
| Never starts a conversation | Jeli only replies to messages addressed to it — and, rarely, to a question the group already answered (R7, capped at 3 per hour per group, can be switched off) |
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

### 5. Knowledge base (Supabase + Gemini)
1. Create a [Supabase](https://supabase.com) project and run [`db/schema.sql`](db/schema.sql) in its SQL editor. Everything goes into a private `jeli` schema, not exposed by Supabase's Data API, and a least-privilege `jeli_app` role.
2. Give the role a password: `alter role jeli_app with login password '<random secret>';`
3. Set `DATABASE_URL` through the **pooler** (IPv4, works from Railway and home networks):
   `postgresql://jeli_app.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres?sslmode=require`
4. Create a Gemini key on [Google AI Studio](https://aistudio.google.com/apikey) → `GEMINI_API_KEY`.

Embeddings: `gemini-embedding-001`, 768 dimensions, normalised. Changing the model or the dimension means re-indexing everything.

### 6. Import the group's history
On a phone in the group: **WhatsApp → the group → ⋮ → More → Export chat → Without media**. Put the `.txt` or `.zip` in `data/exports/` (git-ignored), then:
```bash
# Check the parsing first: nothing is stored or sent anywhere
python -m scripts.import_whatsapp_export data/exports/chat.zip --dry-run
# Import: stores the messages, then chunks and embeds them
python -m scripts.import_whatsapp_export data/exports/chat.zip --chat-id <group id>@g.us --timezone Africa/Bamako
```
- Android and iPhone exports, in any language; system notices, media placeholders and deleted messages are skipped.
- `--timezone`: the exporting phone's timezone (exports carry local times). `--month-first` for US-style dates. `--until YYYY-MM-DD` to stop where live ingestion took over.
- Importing again, or a newer export, only adds what is not stored yet.

From then on, Jeli stores the group's new messages as they arrive and indexes them every few minutes (a conversation is indexed once it has been quiet for 30 minutes).

### 7. Search the history
```bash
python -m scripts.search "When is the bootcamp?"
```
Hybrid retrieval: semantic neighbours (pgvector, cosine) and keyword matches (Postgres full-text with French and English stemming: "échéances" finds "échéance"), merged by reciprocal rank fusion with a small bonus for recent conversations — while always keeping the two semantically closest chunks, so that keyword noise cannot crowd them out (measured: a French question over English transcripts matched only "module" as a keyword and pushed the one right excerpt, the closest by meaning, down to 10th place). Consecutive messages are chunked together (a new chunk after 30 minutes of silence or 1,500 characters), so a question finds the conversation, not a lone "yes, Friday"; each chunk starts with a readable header — the group, the day, who was talking — and near-duplicate chunks (a history imported twice) count once.

**Keeping the memory clean:** `python -m scripts.hygiene` (dry run) then `--apply` merges chat aliases into one id, deletes repeated messages, adds other bots to the ignored authors, switches the deadline finding back on if it was off, and drops the chunks so that the running Jeli indexes everything again in the current format. Measured on 21 Sep: the main group's history had been imported three times under three ids, and half of the excerpts given to the model were repeats.

### 8. Import call recordings
```bash
# A session recording on YouTube: Gemini watches it directly, window by window
python -m scripts.import_recording https://youtu.be/<id> --title "Module 1 class session" --date 2026-09-15
# A recording downloaded from Teams, Drive… (audio or video)
python -m scripts.import_recording data/recordings/session.mp4 --title "…" --date 2026-09-16
# A transcript exported from Teams or Zoom: most accurate, and uses no quota
python -m scripts.import_recording data/recordings/transcript.vtt --title "…" --date 2026-09-16
```
- Gemini transcribes in 15-minute windows. For videos only the window is processed, at low resolution and 0.1 frame per second: about 1,900 tokens per minute, and still enough to read the speaker names Teams shows on screen. Names found in one window are passed to the next.
- Transcripts are cached in `data/transcripts/` (git-ignored): importing again never transcribes twice. `--retranscribe` forces it; `--dry-run` shows the first lines without storing.
- Each transcript segment is stored like a message of the recording (`chat_id = recording:<date>-<title>`) and chunked and embedded like conversations, with the recording's title at the top of each chunk.
- Answers cite the session and the moment — and for YouTube, a link that starts playing there:
  ```
  [2] 🎥 Module 1 class session · 15 Sep 2026 · at 12:34 · Charles B.
      https://youtu.be/<id>?t=754
  ```
- Recordings behind a login (Teams/OneDrive, private Drive) must be downloaded by someone who has access, then imported as a file.

### 9. Ask a question
```bash
python -m scripts.ask "When is the bootcamp?"
```
Illustrative output (names made up):
```
The bootcamp was moved to 25 September, same venue.

📌 Sources
[1] METI cohort · 12 Sep 2026, 14:05 UTC · Awa T., +234 ···55
```

How an answer is built ([`app/answer/rag.py`](app/answer/rag.py)):
1. Hybrid search retrieves the 6 closest conversation chunks.
2. **"I don't know" first:** if even the best chunk is not similar enough (`ANSWER_MIN_SIMILARITY`, 0.60 — measured: group questions score ≥ 0.65, unrelated ones ≤ 0.56), Jeli says so without calling the model.
3. The chunks are rebuilt from their messages, oldest first: authors from `IGNORED_AUTHORS` (e.g. other bots in the group) are left out, phone numbers are masked (`+234 ···55`).
4. Gemini answers **only from those excerpts**, in the question's language, and returns which excerpts it used (structured JSON output).
5. An answer that cites no real excerpt is discarded: Jeli says it doesn't know. The cited excerpts become the sources shown.
6. **Resilience:** models are tried in order (`GEMINI_MODELS`); one that is out of quota or overloaded is skipped for a few minutes. If every model is down, Jeli still points to where the group discussed the question.

Models: `gemini-3.6-flash` with minimal thinking (≈2 s), then `gemini-3.5-flash-lite` and `gemini-flash-lite-latest` (<1 s). Measured: default thinking took 14 s or was overloaded; minimal is fast *and* correct. `GEMINI_API_KEY` takes a comma-separated list of keys (15 or more): each call tries the best model on two keys, then the next models, at most four attempts, keys rotating; a key and model out of quota rests for a few minutes. Measured on 21 Sep with 7 keys: an unbounded fallback tried up to 21 pairs and one answer in ten took over 20 s. The same question asked again within 10 minutes (a jury in a row) is answered from a cache.

### 10. Evaluate answer quality
```bash
python -m scripts.evaluate --show-answers
```
Runs the fixed question set in [`evals/questions.json`](evals/questions.json) against the real knowledge base: questions answered in the chats, questions answered **only in call recordings** (the source must be a recording), general questions the community brief covers, traps where two programmes share vocabulary (hackathon team size vs Wadhwani platform team size), and unrelated questions (must get "I don't know"), in English and French — plus **understanding cases** (`"mode": "understand"`): what the understanding step makes of "recap d'aujourd'hui", "4" after a listing, "en vocal", "Thank you Jeli", "how can you help a visually impaired person?", with the conversation given. Reports latency against the 10-second target. `--only understand` runs one kind of case.

It also checks duplicate detection on real questions re-asked in the group: Jeli must step in for those the group answered, and stay silent for new questions and for questions left unanswered.

**Feedback from members:** a 👍/👎/❤️ reaction on one of Jeli's messages, or a correction ("that's wrong"), is recorded (`jeli.feedback`) as a verdict on that answer — with `message.reaction` among WAHA's `WHATSAPP_HOOK_EVENTS`. Reactions from Jeli itself are dosed: only to real emotion in the group (sad news, a laugh, a success), never to every "thanks", at most 10 an hour per group.

**Images posted in the groups** (a flyer, a screenshot of a schedule) are described by the model and remembered with their caption, so that "when is the Open Hour?" finds the flyer that said it (at most 60 a day).

At startup, Jeli lists the models the key can see and logs the configured ones it cannot (measured: the image models named in the code had been retired, and every image request failed for a day).

What the evaluation caught and fixed: half of the chunks retrieved for a hackathon question were another bot's messages, leaving no usable excerpt once filtered (chunks are now over-fetched, then the best usable ones kept); the model refusing when a rule was relayed by a member rather than an organiser; French questions answered in English when excerpts were English (the answer language is now stated explicitly); and rules of one programme attributed to another (the instructions now name the community's parallel programmes and forbid mixing them).

### Tests
```bash
pytest
```
Every push to `main` runs the tests on GitHub Actions and, when they pass, deploys to Railway ([`.github/workflows/ci.yml`](.github/workflows/ci.yml); needs a Railway project token in the `RAILWAY_TOKEN` repository secret).

---

## Deploy (Railway)

Two services in one Railway project, talking over Railway's private network. Only WAHA gets a public domain (for its dashboard); every webhook call it makes to Jeli is signed.

```
Railway project "jeli"  (region europe-west4, 1 replica each)
├── waha  image devlikeapro/waha:gows · volume /app/.sessions · public domain → port 3000
│         └── webhook ──► http://jeli.railway.internal:8000/waha/webhook
└── jeli  GitHub repo (railway.json: start command, /health check, restart policy)
          └── API ──────► http://waha.railway.internal:3000
```

With the [Railway CLI](https://docs.railway.com/guides/cli) (`npm i -g @railway/cli`, then `railway login`):

**1. Project and `waha` service**
```bash
railway init --name jeli
railway add --service waha --image devlikeapro/waha:gows \
  --variables "PORT=3000" \
  --variables "WHATSAPP_DEFAULT_ENGINE=GOWS" \
  --variables "WHATSAPP_START_SESSION=default" \
  --variables "WAHA_PRINT_QR=false" \
  --variables "WAHA_CLIENT_BROWSER_NAME=Chrome" \
  --variables "WAHA_CLIENT_DEVICE_NAME=Windows" \
  --variables "WAHA_SESSION_CONFIG_IGNORE_STATUS=true" \
  --variables "WAHA_SESSION_CONFIG_IGNORE_CHANNELS=true" \
  --variables "WAHA_SESSION_CONFIG_IGNORE_BROADCAST=true" \
  --variables "WAHA_BASE_URL=http://\${{RAILWAY_PRIVATE_DOMAIN}}:3000" \
  --variables "WHATSAPP_HOOK_EVENTS=message,session.status,poll.vote,message.reaction" \
  --variables "WAHA_DASHBOARD_USERNAME=admin" \
  --variables "WHATSAPP_SWAGGER_USERNAME=admin"
# Secrets: generate each with python -c "import secrets; print(secrets.token_urlsafe(32))"
railway variable set WAHA_API_KEY WHATSAPP_HOOK_HMAC_KEY WAHA_DASHBOARD_PASSWORD WHATSAPP_SWAGGER_PASSWORD ...
```
- `PORT=3000` matters: Railway injects its own `PORT`, which WAHA follows.

**2. `jeli` service**, with *reference variables* so secrets are defined once, in `waha`:
```bash
railway add --service jeli --repo professeurp2/jeli --branch main \
  --variables "PORT=8000" \
  --variables "WAHA_URL=http://\${{waha.RAILWAY_PRIVATE_DOMAIN}}:\${{waha.PORT}}" \
  --variables "WAHA_API_KEY=\${{waha.WAHA_API_KEY}}" \
  --variables "WAHA_WEBHOOK_HMAC_KEY=\${{waha.WHATSAPP_HOOK_HMAC_KEY}}"
railway variable set "WHATSAPP_HOOK_URL=http://\${{jeli.RAILWAY_PRIVATE_DOMAIN}}:\${{jeli.PORT}}/waha/webhook" --service waha
```
Later: `WHATSAPP_GROUP_IDS=<cohort group id>` on `jeli`, `GEMINI_API_KEY`, `DATABASE_URL`.

**3. Region, volume, domain**
```bash
railway scale --service waha europe-west4-drams3a=1 sfo=0   # use the region ids shown by `railway scale`
railway scale --service jeli europe-west4-drams3a=1 sfo=0
railway volume --service waha add --mount-path /app/.sessions
railway domain --service waha --port 3000
```
Run **exactly one replica of WAHA**: two instances of the same WhatsApp session would get the number flagged. Move the region *before* adding the volume (a volume lives in one region).

**4. Check**
- `railway logs --service jeli` shows `WhatsApp adapter enabled through WAHA` then `WhatsApp session default is …`: Jeli reached WAHA with the right key.
- WAHA's `session.status` events appear in Jeli's logs with `POST /waha/webhook 200`: WAHA reached Jeli with a valid signature.

**5. Link the number** (only after the warm-up, see [Keeping Jeli's number safe](#keeping-jelis-number-safe)): open `https://<waha-domain>/dashboard` (user `admin`, password `WAHA_DASHBOARD_PASSWORD`, API key `WAHA_API_KEY`), restart the `default` session if it shows `FAILED` (an unscanned QR code expires), and scan the QR code from Jeli's phone.

**Deploying new code:** if the repo is linked without Railway's GitHub app, pushes do not trigger a deploy: run `railway redeploy --service jeli --from-source`.

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
| `DATABASE_URL` | knowledge base | Supabase Postgres through the pooler, as role `jeli_app` (see Setup §5) |
| `GEMINI_API_KEY` | knowledge base | Google AI Studio key (embeddings and answers) |
| `GEMINI_MODELS` | no | Answer models, in fallback order. Default `gemini-3.6-flash,gemini-3.5-flash-lite,gemini-flash-lite-latest` |
| `ANSWER_MIN_SIMILARITY` | no | Below it, "I don't know" without calling the model. Default 0.60 |
| `TRANSCRIPTION_MODELS` | no | Models that transcribe recordings, in fallback order. Default `gemini-3.6-flash,gemini-3.5-flash-lite` |
| `IGNORED_AUTHORS` | no | Comma-separated authors never used in answers (other bots). Display names or phone numbers; matched against the WhatsApp id too |
| `DUPLICATE_DETECTION` | no | Point to earlier answers when the group re-asks a question (R7). Default `true` |
| `DUPLICATE_MIN_SIMILARITY` | no | Similarity needed before even checking. Default 0.70 |
| `DUPLICATE_REPLIES_PER_HOUR` | no | Uninvited replies per group per hour. Default 3 |
| `DASHBOARD_USERS` | no | Accounts of `/dashboard`, `name:salt:hash,…`, made by `python -m scripts.dashboard_users`. Empty: no dashboard |
| `DASHBOARD_SECRET` | with `DASHBOARD_USERS` | Random string signing the dashboard's sessions. Empty: members sign in again after each restart |
| `DAILY_DIGEST_TIME` | no | `HH:MM` (UTC) to post the daily digest in each group of `WHATSAPP_GROUP_IDS`. Empty: off |
| `TEAM_REPORT_TIME` | no | Day and time (UTC) of the weekly team report, e.g. `mon 07:00`. Empty: off |
| `TEAM_NUMBERS` | with `TEAM_REPORT_TIME` | Team members' WhatsApp numbers, comma-separated. Server only |
| `ORGANISERS` | no | The community's organisers, beyond the groups' admins: names and/or numbers ("Diane +250 …"), comma-separated. Server only; editable on the dashboard |
| `DAILY_DIGEST_LANGUAGE` | no | `en` or `fr`. Default `en` |
| `CHAT_LABELS` | no | Readable chat names in sources: `chat-id=Name;other-id=Other name` |
| `EXPORT_TIMEZONE` | no | Default timezone of imported exports. Default `UTC` |
| `INDEX_INTERVAL_SECONDS` | no | How often live messages are indexed (once quiet for 3 minutes). Default 120 |
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
├── serve.py           # production entry point (one dual-stack socket)
├── web/               # the dashboard: pages.py, auth.py (sign-in, sessions, CSRF), ui.py (look), chart.py
├── control/           # runtime.py (team settings), apply.py, activities.py + setup.py (background work), guard.py (misuse), schedule.py, words.py
├── answer/            # responder.py, rag.py, catchup.py, recaps.py, deadlines.py, intents.py, llm.py (Gemini + fallback), prompts.py, citations.py, language.py
├── ingest/            # whatsapp_export.py, transcribe.py, chunker.py, live.py
├── kb/                # embeddings.py (Gemini), store.py (pgvector), indexer.py, search.py
└── jobs/              # daily_digest.py, team_report.py (one run each; scheduled by control/activities.py)
scripts/               # import_whatsapp_export, import_recording, recap_recording, extract_deadlines, dashboard_users, search, ask, evaluate, forget
evals/questions.json   # fixed question set for answer quality
db/schema.sql          # knowledge base schema and least-privilege role
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

**What Jeli stores** (Supabase, `jeli` schema, EU region):

| Stored | Not stored |
|---|---|
| Group messages: author's display name and WhatsApp id, time, text | Direct messages to Jeli (private questions) |
| Conversation chunks of those messages, and their embeddings | Media, voice notes, deleted messages, system notices |
| | Anything from groups outside `WHATSAPP_GROUP_IDS` |

Texts leave that database only to Google's Gemini API, to compute embeddings (and, from Day 3, answers). Nothing is sold or shared.

**Delete it:**
```bash
python -m scripts.forget --chat-id <group id>@g.us --yes   # one group
python -m scripts.forget --all --yes                       # everything
```
