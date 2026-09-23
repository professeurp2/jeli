"""WhatsApp adapter through WAHA, a self-hosted WhatsApp Web gateway (unofficial).

WAHA keeps a WhatsApp session open for Jeli's dedicated number and forwards every message it
sees — in the group and in direct messages — to our webhook. Replies go back through WAHA's API.
The official Cloud API cannot read groups, which the challenge requires.

Because the channel is unofficial, the number can be restricted if it behaves like a machine.
Jeli therefore never starts a conversation, only answers when addressed, and paces itself
(see app/adapters/pacing.py).
"""

import asyncio
import base64
import dataclasses
import hashlib
import hmac
import json
import logging
import random
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.adapters import Ingest, Respond
from app.adapters.pacing import SendSpacer, SlidingWindowLimiter, reading_delay, typing_duration
from app.answer.citations import NUMBER_OF_LID, is_ignored, poll_text
from app.answer.language import TEXTS, detect_language
from app.answer.react import is_correction
from app.answer import illustrator
from app.answer.illustrator import asks_for_image
from app.answer.voice import MAX_SPOKEN_CHARS, audio_mimetype, audio_seconds, asks_for_voice, sources, spoken, without_voice_request
from app.config import Settings
from app.control.admin import is_super_admin
from app.control.guard import Guard, REPEAT_WINDOW, member_key as guard_member_key
from app.models import Attachment, IncomingMessage, Reply

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/waha/webhook"
SEEN_IDS_KEPT = 1000
# After a reconnection WAHA may deliver a backlog: answering old mentions in a burst looks like a bot.
MAX_REPLY_AGE_SECONDS = 600
# WhatsApp errors meaning "too many new contacts": they lift on their own, re-linking makes it worse.
RESTRICTION_ERRORS = ("463", "475")
# Status updates and channels are not conversations.
IGNORED_CHAT_SUFFIXES = ("@broadcast", "@newsletter")
TEXT_MENTION = re.compile(r"@(\d{5,})")
# Admin commands that only team members can use.
ADMIN_COMMAND = re.compile(r"^/(silence|mute|pause|resume|unsilence|unmute)\b(.*)$", re.IGNORECASE | re.DOTALL)
# Self-introduction patterns: higher voice rate for first contact.
_INTRO = re.compile(
    r"\b(?:je\s+me\s+pr[eé]sente|je\s+m[''']appelle|je\s+suis\s+nouveau|je\s+rejoins|"
    r"permit\s+me\s+to\s+introduce|my\s+name\s+is|i\s+(?:am|'m)\s+new|just\s+joined|"
    r"first\s+(?:time|message|day)|nouveau\s+(?:ici|membre)|bonne\s+arriv[eé]e|"
    r"glad\s+to\s+(?:join|be\s+here)|ravi\s+de\s+(?:rejoindre|vous\s+retrouver))\b",
    re.IGNORECASE,
)
ADMIN_DURATION = re.compile(r"(\d+)\s*(h|hours?|heures?|m|min|minutes?)", re.IGNORECASE)
# Short affirmative answers that confirm a pending image offer.
_YES = re.compile(
    r"^\s*(?:oui|yes|yep|yeah|ok(?:ay)?|bien\s+s[uû]r|carrement|absolument|go|vas-y|allons-y|affirmative|of\s+course|sure|please|s[''']il\s+te\s+pla[iî]t)\s*[!.]*\s*$",
    re.IGNORECASE,
)
# Pending image offers expire after this many seconds (one follow-up window).
_IMAGE_OFFER_TTL = 300
# Documents Jeli keeps when a member shares them in a group.
DOCUMENT_TYPES = (".pdf", ".docx", ".txt", ".md")
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
# Shown as "recording audio…" for as long as a person would take to record the voice note, at most.
VOICE_RECORDING_SECONDS = 12
# Reactions, chosen by the model from the message's feeling (app/answer/emotion.py). In a
# conversation with Jeli: from a clear emotion on. On group messages not addressed to Jeli: only a
# strong, real emotion (grief, a laugh, a success), never a "thanks" or a "hello" (measured:
# reacting to every one of those in a 240-member group is noise, and looks like a machine), within
# a daily budget of model calls; at most REACTIONS_PER_HOUR per chat, never in silent groups.
CONVERSATION_REACTION_STRENGTH = 2
GROUP_REACTION_STRENGTH = 2
GROUP_REACTION_EMOTIONS = {"sadness", "humor", "joy", "pride"}
GROUP_FEELINGS_PER_DAY = 300
REACTIONS_PER_HOUR = 10
# Jeli answers with a sticker when the feeling is strong and the groups have one that says it. Only
# their own stickers, only these feelings, and a few times an hour per chat: an emoji on a message
# is a nod, a sticker is joining in, and a bot posting images all day is tiring — and the kind of
# volume WhatsApp restricts.
STICKER_STRENGTH = 3
STICKER_EMOTIONS = {"joy", "humor", "pride", "sadness", "gratitude", "love", "encouragement"}
STICKERS_PER_HOUR = 4
# Images posted in a group (flyers, screenshots of a schedule) are described and remembered, so
# that questions find them; at most this many a day, to spare the quota.
GROUP_IMAGES_PER_DAY = 60
# What a member says of an answer with a reaction on Jeli's message.
FEEDBACK_REACTIONS = {"👍": "good", "❤️": "good", "🙏": "good", "💯": "good", "👎": "bad", "❌": "bad", "😕": "bad"}
SENT_KEPT = 300
# At most this many id-to-number pairs learned from WhatsApp, paged (see Waha.learn_numbers).
MAX_LIDS = 10_000

router = APIRouter()


def verify_signature(body: bytes, signature: str | None, key: str) -> bool:
    """Check WAHA's X-Webhook-Hmac header: hex HMAC-SHA512 of the raw body."""
    if not signature or not key:
        return False
    expected = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


def user_part(jid: str | None) -> str:
    """'22370000000:12@s.whatsapp.net' -> '22370000000': drop the device and the server."""
    if not jid:
        return ""
    return jid.split("@", 1)[0].split(":", 1)[0]


def _mentioned_ids(data: Any) -> set[str]:
    """Collect the ids under any 'mention…' key of the engine's raw message.

    Field names differ between WAHA engines, so the raw data is searched rather than addressed.
    Quoted messages are skipped: a mention inside the message being replied to is not a new mention.
    """
    found = set()
    if isinstance(data, dict):
        for key, value in data.items():
            lowered = key.lower()
            if "quoted" in lowered:
                continue
            if "mention" in lowered and isinstance(value, list):
                found.update(user_part(item) for item in value if isinstance(item, str))
            else:
                found |= _mentioned_ids(value)
    elif isinstance(data, list):
        for item in data:
            found |= _mentioned_ids(item)
    return found


def _lid_in(body: Any) -> str:
    """The account id in whatever shape WAHA answers with: a string, a list, or an object whose
    key is `lid`, `_serialized` or `id`. Its wording has changed before; its meaning has not."""
    if isinstance(body, str):
        return body if "@lid" in body or body.isdigit() else ""
    if isinstance(body, list):
        return next((found for item in body if (found := _lid_in(item))), "")
    if isinstance(body, dict):
        for key in ("lid", "_serialized", "id"):
            found = _lid_in(body.get(key))
            if found:
                return found
    return ""


def _author(payload: dict) -> str:
    data = payload.get("_data") or {}
    info = data.get("Info") or {}
    # GOWS, then NOWEB, then WEBJS naming.
    return info.get("PushName") or data.get("pushName") or data.get("notifyName") or "Someone"


def _find_poll(data: Any) -> tuple[str, list[str]] | None:
    """A poll's question and options, wherever the engine puts them ("pollCreationMessageV3"…)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if "quoted" in key.lower() or key == "replyTo":
                continue  # a reply to a poll is not a new poll
            if "poll" in key.lower() and isinstance(value, dict):
                question = value.get("name") or value.get("title") or value.get("question")
                options = value.get("options") or value.get("pollOptions") or []
                names = [o.get("optionName") or o.get("name") if isinstance(o, dict) else str(o) for o in options]
                names = [n for n in names if n]
                if question and names:
                    return str(question), names
            found = _find_poll(value)
            if found:
                return found
        question, options = data.get("pollName"), data.get("pollOptions")
        if question and isinstance(options, list):
            names = [o.get("name") if isinstance(o, dict) else str(o) for o in options]
            return str(question), [n for n in names if n]
    elif isinstance(data, list):
        for item in data:
            found = _find_poll(item)
            if found:
                return found
    return None


def _voice_note(payload: dict) -> dict | None:
    """The audio of a voice note (or any audio a member sends), None for other messages."""
    media = payload.get("media") or {}
    if payload.get("hasMedia") and media.get("url") and (media.get("mimetype") or "").startswith("audio/"):
        return media
    return None


def _image_media(payload: dict) -> dict | None:
    """A photo or image the member shared (screenshot, chart, table …), None for other media."""
    media = payload.get("media") or {}
    if payload.get("hasMedia") and media.get("url") and (media.get("mimetype") or "").startswith("image/"):
        return media
    return None


def parse_message(event: dict, bot_name: str) -> IncomingMessage | None:
    """Turn a WAHA `message` event into an IncomingMessage; None for anything Jeli should not process."""
    if event.get("event") != "message":
        return None
    payload = event.get("payload") or {}
    chat_id = payload.get("from") or ""
    text = (payload.get("body") or "").strip()
    poll = _find_poll(payload.get("_data")) or _find_poll({k: v for k, v in payload.items() if k != "_data"})
    if poll:
        text = poll_text(*poll)  # a poll: its question and options, to be remembered with its votes
    voice = _voice_note(payload)
    image = _image_media(payload)
    is_sticker = payload.get("type") == "sticker"
    if payload.get("fromMe") or not chat_id or chat_id.endswith(IGNORED_CHAT_SUFFIXES) or not (text or voice or image or is_sticker):
        return None

    me = event.get("me") or {}
    bot_ids = {user_part(me.get("id")), user_part(me.get("lid"))} - {""}
    is_private = not chat_id.endswith("@g.us")

    mentions = _mentioned_ids(payload.get("_data")) | set(TEXT_MENTION.findall(text))
    mentioned = bool(bot_ids & mentions)
    reply_to = payload.get("replyTo") or {}
    replied_to_bot = user_part(reply_to.get("participant")) in bot_ids
    # A reply to, or a mention of, another member: not for Jeli, even in a conversation with it.
    replied_to_other = bool(reply_to.get("participant")) and not replied_to_bot
    talks_to_someone_else = replied_to_other or bool(mentions - bot_ids)
    named = re.match(rf"\s*{re.escape(bot_name)}\b[\s,:!?.-]*", text, flags=re.IGNORECASE) if bot_name else None
    command = text.startswith("/")

    for bot_id in bot_ids:
        text = text.replace(f"@{bot_id}", "")
    if named:
        text = text[named.end():]
    # GOWS engine uses the display name in the body ("@Jeli_bot") instead of the number.
    # Strip any remaining @BotName or @BotName_suffix so the question text is clean.
    if bot_name:
        text = re.sub(rf"@{re.escape(bot_name)}(?:_\w+)?", "", text, flags=re.IGNORECASE)

    text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()

    # When the member's own text is empty or a bare punctuation mark after stripping the mention,
    # fall back to the body of the quoted/replied-to message as the question context.
    quoted_body = (reply_to.get("body") or "").strip()
    quoted_context = ""
    if not text or (len(text) <= 2 and not text.startswith("/")):
        text = quoted_body
    elif quoted_body:
        # Member typed a real question AND quoted an older message: keep both so the LLM
        # knows what "ça" / "this" / "ce message" refers to.
        quoted_context = quoted_body[:300]

    return IncomingMessage(
        platform="whatsapp",
        chat_id=chat_id,
        message_id=payload["id"],
        author=_author(payload),
        author_id=payload.get("participant") or chat_id,
        text=text,
        sent_at=datetime.fromtimestamp(int(float(payload["timestamp"])), tz=timezone.utc),
        is_private=is_private,
        addressed_to_bot=is_private or mentioned or replied_to_bot or bool(named) or command,
        talks_to_someone_else=talks_to_someone_else,
        voice_url=voice["url"] if voice else None,
        voice_mimetype=voice.get("mimetype", "") if voice else "",
        reply_by_voice=asks_for_voice(text),
        quoted_context=quoted_context,
        image_url=image["url"] if image else None,
        image_mimetype=image.get("mimetype", "") if image else "",
        is_sticker=is_sticker,
    )


def parse_shared_document(event: dict) -> dict | None:
    """A document a member shared (PDF, Word, text), with where to download it; None otherwise."""
    if event.get("event") != "message":
        return None
    payload = event.get("payload") or {}
    media = payload.get("media") or {}
    chat_id = payload.get("from") or ""
    filename = (media.get("filename") or "").strip()
    if payload.get("fromMe") or not payload.get("hasMedia") or not media.get("url") or not chat_id.endswith("@g.us"):
        return None
    if not filename.lower().endswith(DOCUMENT_TYPES):
        return None
    return {
        "chat_id": chat_id,
        "url": media["url"],
        "filename": filename,
        "mimetype": media.get("mimetype") or "",
        "author": _author(payload),
        "sent_at": datetime.fromtimestamp(int(float(payload.get("timestamp") or time.time())), tz=timezone.utc),
        "caption": (payload.get("body") or "").strip(),
    }


class Waha:
    def __init__(self, settings: Settings, respond: Respond, ingest: Ingest | None = None):
        self.session = settings.waha_session
        self.respond = respond
        # Called with every accepted message, to remember the group's conversation.
        self.ingest = ingest
        self.hmac_key = settings.waha_webhook_hmac_key
        self.bot_name = settings.bot_name
        self.groups = settings.whatsapp_groups
        self._http = httpx.AsyncClient(
            base_url=settings.waha_url.rstrip("/"),
            headers={"X-Api-Key": settings.waha_api_key},
            timeout=15,
        )
        # WAHA retries webhooks that fail: remember recent message ids to answer only once.
        self._seen: OrderedDict[str, None] = OrderedDict()
        self.user_limiter = SlidingWindowLimiter(settings.whatsapp_user_limit, settings.whatsapp_user_window_seconds)
        self.hourly_limiter = SlidingWindowLimiter(settings.whatsapp_hourly_limit, 3600)
        # A member's share of answers for the day (set from the dashboard, 0: no limit). Past it,
        # Jeli says so once, kindly, and carries on in writing: no voice note, which is what costs.
        # It never silences anyone — the answer always follows the sentence.
        self.member_daily_limit: int = settings.member_daily_limit
        self.daily_limiter = SlidingWindowLimiter(settings.member_daily_limit, 86_400)
        self._daily_notice = SlidingWindowLimiter(1, 86_400)  # the sentence is said once a day
        self.spacer = SendSpacer(settings.whatsapp_min_send_interval_seconds)
        # Set from WAHA's session.status events: Jeli stays silent while the session is not WORKING.
        self.paused = False
        self.status: str | None = None
        # Tracks the last time Jeli explained a refusal to each member (to avoid repeating every message).
        self._last_explained: dict[str, float] = {}
        # Deferred answers for repeated messages: answered after the repeat window expires.
        self._pending_repeats: dict[str, asyncio.Task] = {}
        # Set by the team from the dashboard: Jeli keeps remembering the groups but sends nothing.
        self.suspended = False
        # Groups where Jeli listens and ingests, but never replies (listen-only / silent mode).
        self.silent_groups: set[str] = set()
        # Team members' numbers (digits only): only they can run admin commands.
        self.admin_numbers: list[str] = settings.team_number_list
        # The one person who may steer Jeli in plain words (app/control/admin.py); "" : nobody.
        self.super_admin_number: str = settings.super_admin_number
        self.admin = None  # set at startup when a model and the settings are available
        # Jeli's own WhatsApp ids, as WAHA reports them: needed to read a mention of itself in a
        # message that did not arrive through a webhook (see remember_history).
        self._me: dict = {}
        # Picture URL to set at startup (e.g. a King Julien image). Empty: no change.
        self._bot_picture_url: str = settings.bot_picture_url
        # Spots and silences members who misuse Jeli (floods, repeats, manipulation attempts).
        self.guard: Guard | None = None
        # Whether a group message continues a conversation with Jeli (set by the responder).
        self.follow_up = None
        # Last message Jeli sent per chat, so it can delete it if a correction comes in.
        self._last_sent: dict[str, str] = {}  # chat_id → message_id
        # Keeps a document shared in a group: (filename, data, mimetype, shared_by, shared_at, chat_id).
        self.on_document = None
        # Records a vote in a poll: (poll_id, voter, options).
        self.on_vote = None
        # Listens to voice notes and speaks answers (app/answer/voice.py); None: text only.
        self.voice = None
        # Feels the emotion of a message, emoji or sticker, for a fitting reaction (app/answer/emotion.py).
        self.emotions = None
        # Image generation toggles (set from the dashboard via apply.py).
        self.enabled_images: bool = True          # explicit "génère une image de…" requests
        self.enabled_proactive_images: bool = True  # proactive suggestion after a rich answer
        self.proactive_image_rate: float = 1.0    # fraction of eligible answers that get an offer
        # Probabilistic voice: fraction of messages Jeli answers by voice (0.0 – 1.0).
        self.voice_rate: float = 0.20
        self.voice_intro_rate: float = 0.80
        # Members Jeli has already talked to: first contact triggers voice_intro_rate.
        self._seen_members: set[str] = set()
        # Pending proactive image offers: chat_id → (ImagePrompt, expiry_timestamp).
        self._pending_image_offers: dict[str, tuple] = {}
        # Whether this member was talking with Jeli a moment ago (set by the responder).
        self.in_conversation = None
        # Reads a member's recent conversation back after a restart (set by the responder).
        self.warm = None
        # Records a member's verdict on an answer: (chat_id, message_id, verdict, question, answer).
        self.on_feedback = None
        # Jeli's own messages, to know which answer a reaction or a correction is about.
        self._sent: OrderedDict[str, tuple[str, str, str]] = OrderedDict()  # id → (chat, question, answer)
        self.reaction_limiter = SlidingWindowLimiter(REACTIONS_PER_HOUR, 3600)
        self.sticker_limiter = SlidingWindowLimiter(STICKERS_PER_HOUR, 3600)
        self.enabled_stickers: bool = True  # set from the dashboard (apply.py)
        # The knowledge base, for the stickers the groups use (set at startup in app/main.py).
        self.store = None
        self.feeling_limiter = SlidingWindowLimiter(GROUP_FEELINGS_PER_DAY, 86400)
        self.image_limiter = SlidingWindowLimiter(GROUP_IMAGES_PER_DAY, 86400)
        self._later: set[asyncio.Task] = set()
        self._admins: dict[str, tuple[float, set[str]]] = {}
        # Phone number -> the ids it writes under in the groups (see ids_for_number), cached 1 h.
        self._lids: dict[str, tuple[float, list[str]]] = {}

    def accepts(self, message: IncomingMessage) -> bool:
        """Direct messages are always accepted; groups only if listed in WHATSAPP_GROUP_IDS (when set)."""
        if message.is_private or not self.groups or message.chat_id in self.groups:
            return True
        log.info("Ignoring group %s: not in WHATSAPP_GROUP_IDS", message.chat_id)
        return False

    def first_delivery(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        if len(self._seen) > SEEN_IDS_KEPT:
            self._seen.popitem(last=False)
        return True

    def set_status(self, status: str | None) -> None:
        self.status = status
        self.paused = status != "WORKING"
        # FAILED means the number must be linked again (scan the QR code in the WAHA dashboard).
        log.log(logging.WARNING if self.paused else logging.INFO, "WhatsApp session %s is %s", self.session, status)

    async def sync_status(self) -> None:
        """Read the session status from WAHA at startup; this also checks the connection and the API key."""
        try:
            response = await self._http.get(f"/api/sessions/{self.session}")
            response.raise_for_status()
            session = response.json()
            status = session.get("status")
            self._me = session.get("me") or self._me
        except (httpx.HTTPError, ValueError) as error:
            log.error("Cannot read the session status from WAHA at %s: %r", self._http.base_url, error)
            return
        self.set_status(status)
        if self._bot_picture_url:
            await self._set_profile_picture(self._bot_picture_url)

    async def _set_profile_picture(self, url: str) -> None:
        """Download image from url and set it as Jeli's WhatsApp profile picture."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                img_response = await client.get(url)
                img_response.raise_for_status()
                b64 = base64.b64encode(img_response.content).decode()
            await self._post("/api/my/picture", {"picture": f"data:{img_response.headers.get('content-type', 'image/jpeg')};base64,{b64}"})
            log.info("Profile picture updated from %s", url)
        except Exception as error:
            log.warning("Could not set profile picture: %r", error)

    def _is_admin(self, message: IncomingMessage) -> bool:
        """True when the sender is one of the team members (admin_numbers list)."""
        if not self.admin_numbers:
            return False
        return any(is_super_admin(num, message.author_id, message.author) for num in self._known_numbers(message))

    def _known_numbers(self, message: IncomingMessage) -> list[str]:
        """The sender's id, and the phone number behind it when WhatsApp has told us (learn_numbers)."""
        who = re.sub(r"\D", "", (message.author_id or message.author or "").split("@")[0].split(":")[0])
        number = NUMBER_OF_LID.get(who)
        return [message.author_id or message.author or "", number or ""]

    async def _auto_unsilence(self, chat_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        self.silent_groups.discard(chat_id)
        log.info("Auto-unsilenced group %s after admin timer expired", chat_id)

    async def _try_admin_command(self, message: IncomingMessage) -> bool:
        """Handle /silence [duration] and /resume. Returns True if an admin command was handled."""
        match = ADMIN_COMMAND.match(message.text.strip())
        if not match:
            return False
        verb = match.group(1).lower()
        args = (match.group(2) or "").strip()
        language = detect_language(message.text)
        if verb in ("resume", "unsilence", "unmute"):
            self.silent_groups.discard(message.chat_id)
            reply = TEXTS[language]["admin_resumed"]
        else:
            self.silent_groups.add(message.chat_id)
            dur_match = ADMIN_DURATION.search(args)
            if dur_match:
                amount = int(dur_match.group(1))
                unit = dur_match.group(2)[0].lower()
                seconds = amount * 3600 if unit == "h" else amount * 60
                task = asyncio.create_task(self._auto_unsilence(message.chat_id, seconds))
                self._later.add(task)
                task.add_done_callback(self._later.discard)
                duration_str = (f" for {amount}h" if unit == "h" else f" for {amount} min") if language == "en" else (f" pendant {amount}h" if unit == "h" else f" pendant {amount} min")
            else:
                duration_str = ""
            reply = TEXTS[language]["admin_silenced"].format(duration=duration_str)
        await self.send_text(message.chat_id, reply, reply_to=message.message_id)
        return True

    def may_reply(self, message: IncomingMessage) -> str | None:
        """Anti-ban guards: returns the refusal reason, or None when Jeli may answer.
        Reasons the member caused ('cooling_down', 'oversized', 'repeat', 'flood') trigger an
        explanation; system/team reasons ('silent_group', 'suspended', 'paused', 'age',
        'blocked', 'hourly_limit') stay silent."""
        if message.chat_id in self.silent_groups:
            log.info("Group %s is in silent mode: Jeli listens but does not reply", message.chat_id)
            return "silent_group"
        if self.suspended:
            log.info("Jeli is paused by the team: not answering message %s", message.message_id)
            return "suspended"
        if self.paused:
            log.warning("Session is not WORKING: not answering message %s", message.message_id)
            return "paused"
        age = (datetime.now(timezone.utc) - message.sent_at).total_seconds()
        if age > MAX_REPLY_AGE_SECONDS:
            log.info("Not answering message %s: %d s old (backlog after a reconnection)", message.message_id, age)
            return "age"
        if self.guard:
            refusal = self.guard.check(message) if message.addressed_to_bot else (
                "blocked" if is_ignored(message, self.guard.blocked) else None
            )
            if refusal:
                log.info("Not answering message %s: %s", message.message_id, refusal)
                return refusal
        if not self.user_limiter.allow(message.author_id or message.chat_id):
            log.warning("Member rate limit reached: not answering message %s", message.message_id)
            if self.guard and message.addressed_to_bot:
                self.guard.report(message, "flood")
            return "flood"
        if not self.hourly_limiter.allow("all"):
            log.error("Hourly answer limit reached: Jeli stays silent until the window frees up")
            return "hourly_limit"
        return None

    async def _post(self, path: str, payload: dict) -> dict:
        response = await self._http.post(path, json={"session": self.session, **payload})
        if response.is_error:
            log.error("WAHA %s failed with %s: %s", path, response.status_code, response.text)
            if any(code in response.text for code in RESTRICTION_ERRORS):
                log.error(
                    "WhatsApp is temporarily restricting this number. Do NOT restart, log out or re-link "
                    "the session: the restriction lifts on its own."
                )
        response.raise_for_status()
        try:
            return response.json() or {}
        except ValueError:
            return {}

    async def _post_quietly(self, path: str, payload: dict) -> None:
        """For cosmetic calls (read receipts, typing): a failure must not prevent the answer."""
        try:
            await self._post(path, payload)
        except httpx.HTTPError:
            log.warning("WAHA %s failed, continuing", path)

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None, mentions: list[str] = ()) -> str | None:
        """Send a text message; returns the WAHA message id (for later deletion if needed)."""
        payload = {"chatId": chat_id, "text": text, "linkPreview": False}
        if reply_to:
            payload["reply_to"] = reply_to
        if mentions:
            payload["mentions"] = list(mentions)
        result = await self._post("/api/sendText", payload)
        # WAHA answers with the sent message, but an empty body must not lose what follows it.
        return (result or {}).get("id")

    async def send_voice(self, chat_id: str, audio: bytes, reply_to: str | None = None) -> None:
        """A voice note: WAHA converts the audio to OGG/Opus for WhatsApp."""
        mime = audio_mimetype(audio)
        ext = mime.split("/")[-1]
        payload = {
            "chatId": chat_id,
            "file": {"mimetype": mime, "filename": f"jeli.{ext}", "data": base64.b64encode(audio).decode()},
            "convert": True,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendVoice", payload)

    async def send_image(self, chat_id: str, attachment: Attachment, reply_to: str | None = None) -> None:
        """Send an image that appears inline in WhatsApp (not as a downloadable file)."""
        payload = {
            "chatId": chat_id,
            "file": {
                "mimetype": attachment.mimetype,
                "filename": attachment.filename,
                "data": base64.b64encode(attachment.data).decode(),
            },
            "caption": attachment.caption,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendImage", payload)

    async def send_file(self, chat_id: str, attachment: Attachment, reply_to: str | None = None) -> None:
        payload = {
            "chatId": chat_id,
            "file": {
                "mimetype": attachment.mimetype,
                "filename": attachment.filename,
                "data": base64.b64encode(attachment.data).decode(),
            },
            "caption": attachment.caption,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendFile", payload)

    async def _send_attachment(self, message: IncomingMessage, attachment: Attachment) -> None:
        if self.suspended or self.paused or not self.hourly_limiter.allow("all"):
            return
        await self.spacer.wait_turn()
        if attachment.mimetype.startswith("image/"):
            await self.send_image(message.chat_id, attachment, reply_to=message.message_id)
        else:
            await self.send_file(message.chat_id, attachment, reply_to=message.message_id)

    async def _send_later(self, message: IncomingMessage, pending) -> None:
        """A file being made (a translation): sent when ready, or the reason it could not be."""
        try:
            result = await asyncio.wait_for(pending(), timeout=300)
        except Exception:
            log.exception("A file for message %s could not be made", message.message_id)
            return
        if isinstance(result, Attachment):
            await self._send_attachment(message, result)
        elif result and not self.suspended and self.hourly_limiter.allow("all"):
            await self.spacer.wait_turn()
            await self.send_text(message.chat_id, result, reply_to=message.message_id)

    async def deliver_files(self, message: IncomingMessage, reply: str) -> None:
        attachment = getattr(reply, "attachment", None)
        if attachment:
            await self._send_attachment(message, attachment)
        pending = getattr(reply, "pending", None)
        if pending:
            task = asyncio.get_running_loop().create_task(self._send_later(message, pending))
            self._later.add(task)
            task.add_done_callback(self._later.discard)

    async def keep_document(self, shared: dict) -> None:
        """Download a document shared in a group, and give it to Jeli's documents."""
        if self.on_document is None:
            return
        url = httpx.URL(shared["url"])
        path = url.raw_path.decode()  # WAHA serves it itself: ask our WAHA, whatever host it names
        try:
            response = await self._http.get(path)
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.warning("Cannot download the shared document %s: %r", shared["filename"], error)
            return
        if len(response.content) > MAX_DOCUMENT_BYTES:
            log.info("Shared document %s is too big to keep", shared["filename"])
            return
        try:
            await self.on_document(
                # A short caption names the document; a long one is a message about it.
                shared["filename"], response.content, mimetype=shared["mimetype"],
                title=shared["caption"] if 0 < len(shared["caption"]) <= 60 else "",
                shared_by=shared["author"], shared_at=shared["sent_at"], chat_id=shared["chat_id"],
            )
        except ValueError as error:
            log.info("Shared document %s not kept: %s", shared["filename"], error)

    async def send_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """React to a message with an emoji. Non-critical: never raises.
        WAHA's endpoint is PUT /api/reaction (read from its API specification, 22 Sep): the former
        POST /api/sendReaction answered 404, so no reaction had ever been shown."""
        try:
            await self._put("/api/reaction", {"messageId": message_id, "reaction": emoji})
        except httpx.HTTPError:
            log.warning("WAHA /api/reaction failed, continuing")

    async def _put(self, path: str, payload: dict) -> None:
        response = await self._http.put(path, json={"session": self.session, **payload})
        if response.is_error:
            log.error("WAHA %s failed with %s: %s", path, response.status_code, response.text[:300])
        response.raise_for_status()

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        """Delete one of Jeli's own messages (e.g. a wrong answer). Non-critical: never raises."""
        await self._post_quietly("/api/deleteMessage", {
            "chatId": chat_id,
            "messageId": message_id,
        })

    async def send_reply(self, message: IncomingMessage, reply: str) -> None:
        """Jeli's reply, as WhatsApp shows it: quoting the member's message, or the source message
        itself (WhatsApp's own reference), with its mentions."""
        reaction = getattr(reply, "reaction", "")
        if reaction:
            await self.send_reaction(message.chat_id, message.message_id, reaction)
        sent_id = await self.send_text(
            message.chat_id,
            reply,
            reply_to=getattr(reply, "reply_to", None) or message.message_id,
            mentions=getattr(reply, "mentions", ()),
        )
        # Track Jeli's own message so a subsequent correction can delete it, and so that a
        # reaction on it (👍/👎) is recorded as feedback on this answer.
        if sent_id:
            self._last_sent[message.chat_id] = sent_id
            self._sent[sent_id] = (message.chat_id, message.text[:300], str(reply)[:1000])
            while len(self._sent) > SENT_KEPT:
                self._sent.popitem(last=False)

    async def _feedback(self, chat_id: str, message_id: str, verdict: str, question: str = "", answer: str = "") -> None:
        if self.on_feedback is None:
            return
        try:
            await self.on_feedback(chat_id, message_id, verdict, question, answer)
        except Exception:
            log.exception("Could not record a member's feedback")

    async def reaction(self, payload: dict) -> None:
        """A member reacted to one of Jeli's messages: their verdict on that answer."""
        reaction = payload.get("reaction") or {}
        target, emoji = str(reaction.get("messageId") or ""), str(reaction.get("text") or "")
        if payload.get("fromMe") or not target or target not in self._sent:
            return
        verdict = FEEDBACK_REACTIONS.get(emoji)
        if verdict:
            chat_id, question, answer = self._sent[target]
            await self._feedback(chat_id, target, verdict, question, answer)

    async def handle(self, message: IncomingMessage) -> None:
        if self.suspended:
            return  # the message is still remembered (ingested separately)
        try:
            if self.warm is not None:
                await self.warm(message)  # the conversation so far, after a restart
            # Stickers: Jeli looks at the sticker and reacts to its feeling, in a conversation with it.
            if message.is_sticker:
                if message.is_private or message.addressed_to_bot or (self.in_conversation and self.in_conversation(message)):
                    await self._react(message, CONVERSATION_REACTION_STRENGTH - 1, sticker=True)
                return
            if message.voice_url:
                message = await self._listen(message)
                if message is None:
                    return
            if message.image_url and message.addressed_to_bot:
                message = await self._see(message)
            elif message.image_url and not message.is_private:
                self._remember_image(message)  # a flyer, a screenshot: described and kept, in the background
            if not message.addressed_to_bot and self.follow_up and self.follow_up(message):
                message = dataclasses.replace(message, addressed_to_bot=True)
            if message.addressed_to_bot:
                # Feeling the message while answering it: a "merci ❤️" gets its ❤️ as the reply is typed.
                await asyncio.gather(self._react(message, CONVERSATION_REACTION_STRENGTH), self._converse(message))
            else:
                await self._step_in_if_needed(message)
                # A reaction to real emotion in the group (not to every hello or thanks), dosed.
                if not message.is_private and message.text and message.chat_id not in self.silent_groups \
                        and self.feeling_limiter.allow("groups"):
                    await self._react(message, GROUP_REACTION_STRENGTH, emotions=GROUP_REACTION_EMOTIONS)
        except Exception:
            log.exception("Failed to handle WhatsApp message %s", message.message_id)

    async def _react(self, message: IncomingMessage, min_strength: int, emotions: set[str] | None = None, sticker: bool = False) -> None:
        """React to a message as its feeling calls for (the model reads the words, the emoji, or the
        sticker's image); nothing for a neutral message. Never raises: a reaction is a courtesy."""
        if self.emotions is None or self.paused or self.suspended or message.chat_id in self.silent_groups:
            return
        if self.guard and is_ignored(message, self.guard.blocked):
            return  # a member the team blocked gets nothing, not even a reaction
        try:
            image = None
            if sticker and message.image_url:
                response = await self._http.get(httpx.URL(message.image_url).raw_path.decode())  # served by our WAHA
                response.raise_for_status()
                image = response.content
            if not image and not message.text:
                return
            feeling = await self.emotions.feel(message.text, image=image, mimetype=message.image_mimetype or "image/webp")
            if (
                feeling is None
                or not feeling.reaction
                or feeling.strength < min_strength
                or (emotions is not None and feeling.emotion not in emotions)
                or not self.reaction_limiter.allow(message.chat_id)
            ):
                return
            await self.send_reaction(message.chat_id, message.message_id, feeling.reaction)
            log.info("Reacted %s to message %s (%s, strength %d)", feeling.reaction, message.message_id, feeling.emotion, feeling.strength)
            if sticker and message.image_url and self.store is not None:
                # The groups' own stickers, with what each one says: Jeli answers with theirs.
                await self.store.remember_sticker(message.image_url, feeling.emotion, message.chat_id)
            await self._answer_with_a_sticker(message, feeling)
        except Exception:
            log.exception("Could not react to message %s", message.message_id)

    async def remember_history(self, chat_id: str, since: datetime, limit: int = 300) -> int:
        """Read what a group said since `since` and remember it — without answering any of it.

        Used after a restart or a crash (app/ingest/history.py). WhatsApp still holds the messages
        WAHA could not deliver while Jeli was down; this is how the memory closes the gap. Nothing
        here replies: these messages are hours old, and a bot that wakes up and answers a whole
        morning at once is what gets a number restricted.
        """
        if self.ingest is None:
            return 0
        response = await self._http.get(
            f"/api/{self.session}/chats/{httpx.URL(chat_id).raw_path.decode()}/messages",
            params={
                "limit": limit,
                "downloadMedia": "false",
                "filter.timestamp.gte": int(since.timestamp()),
                "filter.fromMe": "false",
            },
        )
        response.raise_for_status()
        payloads = response.json() or []
        me = self._me
        remembered = 0
        for payload in payloads:
            message = parse_message({"event": "message", "payload": payload, "me": me}, self.bot_name)
            if message is None or message.is_private:
                continue  # private messages are a conversation, not the group's memory
            try:
                await self.ingest(message)
                remembered += 1
            except Exception:
                log.warning("Could not remember an older message of %s", chat_id, exc_info=True)
        return remembered

    async def send_sticker(self, chat_id: str, file_url: str, reply_to: str | None = None) -> None:
        """A sticker, by the URL our own WAHA serves it from. `convert` lets WAHA make the WebP."""
        payload: dict[str, Any] = {"chatId": chat_id, "file": {"url": file_url}, "convert": True}
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendSticker", payload)

    async def _answer_with_a_sticker(self, message: IncomingMessage, feeling) -> None:
        """A sticker back, when a member's own sticker or a strong feeling calls for one.

        An emoji on their message is a nod; a sticker is Jeli joining in. It only ever sends one
        the groups themselves use, and only a few times an hour per chat: a bot that posts images
        all day is both tiring and the kind of volume WhatsApp restricts."""
        if (
            not self.enabled_stickers
            or self.store is None
            or feeling.strength < STICKER_STRENGTH
            or feeling.emotion not in STICKER_EMOTIONS
            or not self.sticker_limiter.allow(message.chat_id)
        ):
            return
        file_url = await self.store.pick_sticker(feeling.emotion)
        if not file_url:
            return  # the groups have never used one for this feeling: nothing to borrow
        try:
            await self.spacer.wait_turn()
            await self.send_sticker(message.chat_id, file_url, reply_to=message.message_id)
            log.info("Answered message %s with a %s sticker", message.message_id, feeling.emotion)
        except Exception:
            log.warning("Could not send a sticker to %s", message.chat_id, exc_info=True)

    def _remember_image(self, message: IncomingMessage) -> None:
        """Describe an image posted in a group and remember the description with its caption, so
        that "when is the Open Hour?" finds the flyer that said it."""
        if self.voice is None or self.ingest is None or not self.image_limiter.allow("all"):
            return
        task = asyncio.get_running_loop().create_task(self._describe_and_remember(message))
        self._later.add(task)
        task.add_done_callback(self._later.discard)

    async def _describe_and_remember(self, message: IncomingMessage) -> None:
        try:
            response = await self._http.get(httpx.URL(message.image_url).raw_path.decode())
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.warning("Cannot download the image from message %s: %r", message.message_id, error)
            return
        description = await self.voice.describe(response.content, message.image_mimetype)
        if not description:
            return
        text = f"[Image: {description}]" + (f"\n{message.text}" if message.text else "")
        await self.ingest(dataclasses.replace(message, text=text, image_url=None))

    async def _explain_refusal(self, message: IncomingMessage, reason: str) -> None:
        """Send a one-time explanation when Jeli can't answer because of the member's own behaviour.
        Silent for team/system reasons (blocked, paused, suspended…) to avoid noise during outages."""
        if reason not in ("cooling_down", "oversized", "repeat", "flood"):
            return
        key = message.author_id or message.author
        now = time.monotonic()
        # For cooling_down / flood: explain only once per hour (not on every message during the cooldown).
        # Default to (now - 3601) so the very first call always sends, regardless of system uptime.
        if reason in ("cooling_down", "flood") and now - self._last_explained.get(key, now - 3601) < 3600:
            return
        self._last_explained[key] = now
        language = detect_language(message.text)
        text = TEXTS[language][f"guard_{reason}" if reason != "flood" else "guard_cooling_down"]
        await self.send_text(message.chat_id, text, reply_to=message.message_id)
        if reason == "repeat":
            # Cancel any existing deferred answer for this member and reschedule:
            # after the repeat window, Jeli answers the last repeated message automatically.
            mk = guard_member_key(message)
            old = self._pending_repeats.pop(mk, None)
            if old:
                old.cancel()
            fresh = dataclasses.replace(message, sent_at=datetime.now(timezone.utc))

            async def _answer_after_window(msg=fresh, _mk=mk) -> None:
                await asyncio.sleep(REPEAT_WINDOW)
                self._pending_repeats.pop(_mk, None)
                await self._converse(msg)

            task = asyncio.create_task(_answer_after_window())
            self._pending_repeats[mk] = task

    async def _see(self, message: IncomingMessage) -> IncomingMessage:
        """Download and describe an image; the description is prepended to the message text so the
        LLM can answer questions about a screenshot, table or chart a member shared."""
        if self.voice is None:
            return message
        try:
            response = await self._http.get(httpx.URL(message.image_url).raw_path.decode())
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.warning("Cannot download the image from message %s: %r", message.message_id, error)
            return message
        description = await self.voice.describe(response.content, message.image_mimetype)
        if not description:
            return message
        prefix = f"[Image: {description}]"
        new_text = f"{prefix}\n{message.text}".strip() if message.text else prefix
        return dataclasses.replace(message, text=new_text, image_url=None)

    async def _listen(self, message: IncomingMessage) -> IncomingMessage | None:
        """A voice note, listened to when it is for Jeli: sent to it, a reply to it, or said in a
        conversation with it. Voice notes between members are never listened to."""
        in_conversation = bool(self.in_conversation and self.in_conversation(message))
        if self.voice is None or self.paused or not (message.addressed_to_bot or in_conversation):
            return None
        if self.guard and is_ignored(message, self.guard.blocked):
            return None
        try:
            response = await self._http.get(httpx.URL(message.voice_url).raw_path.decode())  # served by our WAHA
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.warning("Cannot download the voice note %s: %r", message.message_id, error)
            return None
        heard = await self.voice.listen(response.content, message.voice_mimetype)
        if heard is None:
            # Model unavailable: tell the member warmly so they know what happened.
            if message.addressed_to_bot:
                language = detect_language(message.text or "")
                await self.send_text(message.chat_id, TEXTS[language]["voice_not_heard"], reply_to=message.message_id)
            return None
        if not heard:
            return None  # silence or noise: stay quiet
        message = dataclasses.replace(message, text=heard, voice_url=None, reply_by_voice=True)
        if self.ingest and not message.is_private:
            await self.ingest(dataclasses.replace(message, text=f"🎤 {heard}"))  # the group's memory keeps it too
        return message

    async def _converse(self, message: IncomingMessage) -> None:
        """Answer like a person would: read, type (or record) for a while, then reply (WAHA's
        recommended sequence). Asked by voice, or asked for a voice reply: a voice note."""
        # If a deferred answer was queued (repeat-window), cancel it: the member sent a new message.
        mk = guard_member_key(message)
        pending = self._pending_repeats.pop(mk, None)
        if pending:
            pending.cancel()
        # Admin commands bypass all rate limits, silence and suspension.
        if self._is_admin(message) and await self._try_admin_command(message):
            return
        # The super admin steers Jeli in plain words (app/control/admin.py) — in private, or in a
        # group when speaking to Jeli. In a group it must be addressed to Jeli: otherwise every
        # sentence the super admin says to the cohort would be read as an order. By voice too: the
        # note has already been listened to by the time it gets here.
        if (
            self.admin is not None
            and (message.is_private or message.addressed_to_bot)
            and is_super_admin(self.super_admin_number, message.author_id, message.author)
        ):
            done = await self.admin.handle(message.text or "", actor="super admin")
            if done:
                await self.send_text(message.chat_id, done, reply_to=message.message_id)
                return
        refusal = self.may_reply(message)
        if refusal is not None:
            await self._explain_refusal(message, refusal)
            return
        # Check if this is a short "yes" confirming a pending image offer for this chat.
        if (
            self.enabled_images
            and _YES.match(message.text or "")
            and message.chat_id in self._pending_image_offers
        ):
            ip, expiry = self._pending_image_offers.pop(message.chat_id)
            if time.monotonic() < expiry and self.voice:
                _llm = getattr(self.voice, "llm", None)
                async def _make_offered_image(ip=ip, _llm=_llm) -> "Attachment | None":
                    data = await illustrator.generate(ip.prompt, _llm)
                    if not data:
                        return None
                    return Attachment("jeli.jpg", "image/jpeg", data, caption=ip.caption)
                await self.send_reaction(message.chat_id, message.message_id, "📊")
                asyncio.create_task(self._send_later(message, _make_offered_image))
                return
        # When someone corrects Jeli, acknowledge immediately and delete the wrong message.
        if is_correction(message.text) and message.chat_id in self._last_sent:
            wrong_id = self._last_sent.pop(message.chat_id)
            await self.send_reaction(message.chat_id, message.message_id, "🙏")
            await self.delete_message(message.chat_id, wrong_id)
            log.info("Deleted Jeli's wrong message %s after correction in %s", wrong_id, message.chat_id)
            _, question, answer = self._sent.get(wrong_id, (message.chat_id, "", ""))
            await self._feedback(message.chat_id, wrong_id, "correction", question, answer)
        chat = {"chatId": message.chat_id}
        language = detect_language(message.text or "")
        # This member's share of the day: past it, the answer comes in writing only.
        author = message.author_id or message.author or message.chat_id
        over_cap = self.member_daily_limit > 0 and not self.daily_limiter.allow(author)
        by_voice = self.voice is not None and message.reply_by_voice and not over_cap
        if message.reply_by_voice:
            # Always strip the voice-request clause so the responder sees the real question,
            # regardless of whether the voice module is wired up. Without this, "récap,
            # réponds en vocal" is forwarded intact and the LLM says "je ne peux pas" itself.
            message = dataclasses.replace(message, text=without_voice_request(message.text))
        # Probabilistic voice: reply by voice on a fraction of messages even without being asked.
        # Higher rate for first contact or self-introductions.
        if self.voice and not by_voice and not over_cap:
            author_id = message.author_id or message.author or ""
            is_new = bool(author_id) and author_id not in self._seen_members
            if author_id:
                self._seen_members.add(author_id)
            rate = self.voice_intro_rate if (is_new or bool(_INTRO.search(message.text or ""))) else self.voice_rate
            if rate > 0 and random.random() < rate:
                by_voice = True
        await asyncio.sleep(reading_delay())
        await self._post_quietly("/api/sendSeen", {**chat, "messageIds": [message.message_id]})
        await self._post_quietly("/api/startTyping", chat)
        if over_cap and self._daily_notice.allow(author):
            # Said once a day, and never instead of the answer: the reaction carries the warmth,
            # the sentence explains, the answer follows in writing.
            await self.send_reaction(message.chat_id, message.message_id, "😊")
            await self.send_text(message.chat_id, TEXTS[language]["daily_cap"], reply_to=message.message_id)
            await self.spacer.wait_turn()
        audio = None
        try:
            typing_since = time.monotonic()
            reply = await self.respond(message)
            if reply and by_voice:
                await self._post_quietly(f"/api/{self.session}/presence", {**chat, "presence": "recording"})
                audio = await self.voice.speak(spoken(reply), getattr(reply, "language", "") or language)
            if reply:
                # Answer generation counts as typing time: only wait for what is left.
                busy = min(VOICE_RECORDING_SECONDS, audio_seconds(audio)) if audio else typing_duration(reply)
                await asyncio.sleep(max(0.0, busy - (time.monotonic() - typing_since)))
                # Still "typing…" while other answers go out first.
                await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        if reply:
            voice_sent = audio and await self._send_voice_reply(message, reply, audio)
            if not voice_sent:
                if by_voice and audio is None:
                    # TTS was requested but the speech model failed: tell the member warmly, then give text.
                    await self.send_text(message.chat_id, TEXTS[language]["voice_reply_unavailable"], reply_to=message.message_id)
                    await self.spacer.wait_turn()
                await self.send_reply(message, reply)
            elif len(spoken(reply)) > MAX_SPOKEN_CHARS:
                # Voice note was truncated: send the written reply without source citations
                # (the member already heard the answer; blockquotes would clutter the text).
                clean_text = "\n".join(l for l in reply.splitlines() if not l.startswith(">")).strip()
                clean_reply = Reply(clean_text, reply_to=getattr(reply, "reply_to", None), mentions=getattr(reply, "mentions", ()))
                await self.spacer.wait_turn()
                await self.send_reply(message, clean_reply)
            await self.deliver_files(message, reply)
            if self.voice:
                llm = getattr(self.voice, "llm", None)
                msg_text = message.text
                if llm and asks_for_image(msg_text or ""):
                    if self.enabled_images:
                        topic = illustrator.topic_from_request(msg_text or "") or message.quoted_context
                        if topic:
                            async def make_image() -> Attachment | None:
                                if not self.enabled_images:
                                    return None
                                ip = await illustrator.build_prompt(topic, llm)
                                if not ip:
                                    return None
                                data = await illustrator.generate(ip.prompt, llm)
                                if not data:
                                    return None
                                return Attachment("jeli.jpg", "image/jpeg", data, caption=ip.caption)

                            asyncio.create_task(self._send_later(message, make_image))
                elif llm and self.enabled_proactive_images and (self.proactive_image_rate >= 1.0 or random.random() < self.proactive_image_rate):
                    reply_text = reply
                    _msg_snap = message
                    _lang_snap = language

                    async def _offer_image_if_useful() -> None:
                        if not self.enabled_proactive_images:
                            return
                        ip = await illustrator.suggest_if_useful(msg_text or "", reply_text, llm)
                        if not ip:
                            return
                        # Store the prompt and send a short offer instead of generating immediately.
                        self._pending_image_offers[_msg_snap.chat_id] = (ip, time.monotonic() + _IMAGE_OFFER_TTL)
                        offer_text = TEXTS[_lang_snap]["image_offer"]
                        await self.spacer.wait_turn()
                        await self.send_text(_msg_snap.chat_id, offer_text)

                    task = asyncio.create_task(_offer_image_if_useful())
                    self._later.add(task)
                    task.add_done_callback(self._later.discard)

    async def _send_voice_reply(self, message: IncomingMessage, reply: str, audio: bytes) -> bool:
        """The answer as a voice note. False when it could not be sent: the text goes instead."""
        try:
            await self.send_voice(message.chat_id, audio, reply_to=message.message_id)
        except httpx.HTTPError:
            log.warning("Voice note for message %s not sent: answering in writing", message.message_id)
            return False
        return True

    async def _step_in_if_needed(self, message: IncomingMessage) -> None:
        """A message not addressed to Jeli: it speaks only when the responder finds that the group
        already answered this question (R7), and within the same anti-ban limits."""
        reply = await self.respond(message)
        if not reply or self.may_reply(message) is not None:
            return
        chat = {"chatId": message.chat_id}
        await asyncio.sleep(reading_delay())
        await self._post_quietly("/api/startTyping", chat)
        try:
            await asyncio.sleep(typing_duration(reply))
            await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        await self.send_reply(message, reply)

    async def remind(self, chat_id: str, text: str, reply_to: str | None = None, mentions: list[str] = ()) -> bool:
        """A reminder a member asked for: in their chat, replying to their request, within the same
        limits as every message. False when it could not go now (paused, session down, limit)."""
        if self.suspended or self.paused or not self.hourly_limiter.allow("all"):
            return False
        chat = {"chatId": chat_id}
        await self._post_quietly("/api/startTyping", chat)
        try:
            await asyncio.sleep(typing_duration(text))
            await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        await self.send_text(chat_id, text, reply_to=reply_to, mentions=list(mentions))
        return True

    async def post(self, chat_id: str, text: str) -> bool:
        """A message Jeli sends on its own schedule (daily digest, weekly report), within the same limits.
        Returns False when it was not sent: session not WORKING or hourly limit reached."""
        if self.suspended or self.paused or not self.hourly_limiter.allow("all"):
            return False
        chat = {"chatId": chat_id}
        await self._post_quietly("/api/startTyping", chat)
        try:
            await asyncio.sleep(typing_duration(text))
            await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        await self.send_text(chat_id, text)
        return True

    async def private_chat(self, number: str) -> str | None:
        """The chat id of a number, if it is on WhatsApp. Writing to numbers that are not is a
        classic spam signal, so Jeli checks first."""
        try:
            response = await self._http.get("/api/contacts/check-exists", params={"phone": number, "session": self.session})
            response.raise_for_status()
            found = response.json()
        except (httpx.HTTPError, ValueError) as error:
            log.warning("Cannot check the number ending in %s on WhatsApp: %r", number[-2:], error)
            return None
        return found.get("chatId") if found.get("numberExists") else None

    async def post_private(self, number: str, text: str) -> bool:
        """A private message to a team member (the weekly report), within the same limits."""
        if self.suspended or self.paused:
            return False
        chat_id = await self.private_chat(number)
        if chat_id is None:
            log.warning("The number ending in %s is not on WhatsApp: no report sent", number[-2:])
            return False
        return await self.post(chat_id, text)

    # --- For the dashboard's WhatsApp page -------------------------------------------------------

    async def me(self) -> dict | None:
        """Jeli's own account ({"id", "pushName"}) while linked, else None."""
        try:
            response = await self._http.get(f"/api/sessions/{self.session}/me")
            response.raise_for_status()
            return response.json() or None
        except (httpx.HTTPError, ValueError):
            return None

    async def profile_picture(self, chat_id: str) -> bytes | None:
        """The profile picture of a chat or of Jeli itself, as image bytes, if it has one."""
        try:
            response = await self._http.get(
                "/api/contacts/profile-picture", params={"contactId": chat_id, "session": self.session}
            )
            response.raise_for_status()
            url = (response.json() or {}).get("profilePictureURL")
            if not url:
                return None
            async with httpx.AsyncClient(timeout=15) as client:
                picture = await client.get(url)
                picture.raise_for_status()
                return picture.content
        except (httpx.HTTPError, ValueError):
            return None

    async def qr_code(self) -> bytes | None:
        """The QR code to link Jeli's phone, as a PNG, while the session waits for it."""
        try:
            response = await self._http.get(f"/api/{self.session}/auth/qr", headers={"Accept": "image/png"})
            response.raise_for_status()
            return response.content if response.headers.get("content-type", "").startswith("image/") else None
        except httpx.HTTPError:
            return None

    async def request_code(self, phone: str) -> str | None:
        """A pairing code to type on Jeli's phone (Linked devices → Link with phone number instead)."""
        try:
            response = await self._http.post(f"/api/{self.session}/auth/request-code", json={"phoneNumber": phone})
            response.raise_for_status()
            return (response.json() or {}).get("code")
        except (httpx.HTTPError, ValueError) as error:
            log.error("Cannot get a pairing code: %r", error)
            return None

    async def restart_session(self) -> bool:
        """Start the connection again (a new QR code when the number is not linked)."""
        try:
            response = await self._http.post(f"/api/sessions/{self.session}/restart")
            response.raise_for_status()
        except httpx.HTTPError as error:
            log.error("Cannot restart the WhatsApp session: %r", error)
            return False
        await self.sync_status()
        return True

    async def group_names(self) -> dict[str, str]:
        """The groups Jeli's number is in: id → name."""
        try:
            response = await self._http.get(f"/api/{self.session}/chats/overview", params={"limit": 200})
            response.raise_for_status()
            chats = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return {
            chat["id"]: chat.get("name") or chat["id"]
            for chat in chats
            if isinstance(chat, dict) and str(chat.get("id", "")).endswith("@g.us")
        }

    async def learn_numbers(self, limit: int = 1000) -> int:
        """Learn which phone number each account id belongs to, from WhatsApp itself.

        Without this, everyone named by number in the team's settings is invisible in the groups:
        a muted bot keeps being quoted, an organiser's announcement is not one, a teammate's admin
        command is not obeyed. Names still work; numbers did not (see app/answer/citations.py).
        """
        try:
            # WAHA's own note: reading the groups is what fills the id-to-number mapping. Without
            # this first call, the list below is empty on a fresh session.
            await self._http.get(f"/api/{self.session}/groups", params={"limit": 50})
        except httpx.HTTPError:
            log.info("Could not read the groups before learning their ids", exc_info=True)
        learned = 0
        # Page through: measured 23 Sep, the first page came back exactly full, which means there
        # were more — and the member missing from it is the one who cannot sign in.
        for offset in range(0, MAX_LIDS, limit):
            try:
                response = await self._http.get(
                    f"/api/{self.session}/lids", params={"limit": limit, "offset": offset}
                )
                response.raise_for_status()
                pairs = response.json() or []
            except (httpx.HTTPError, ValueError):
                log.warning("Could not learn which numbers the groups' ids belong to", exc_info=True)
                break
            pairs = pairs if isinstance(pairs, list) else []
            for pair in pairs:
                lid = re.sub(r"\D", "", str(pair.get("lid", "")).split("@")[0])
                number = re.sub(r"\D", "", str(pair.get("pn") or pair.get("phoneNumber") or "").split("@")[0])
                if lid and number:
                    NUMBER_OF_LID[lid] = number
                    learned += 1
            if len(pairs) < limit:
                break
        log.info("Learned the number behind %d of the groups' ids", learned)
        return learned

    async def ids_for_number(self, digits: str) -> list[str]:
        """Every id this phone number writes under, the number itself included.

        WhatsApp no longer puts phone numbers in group messages: participants arrive as a "LID"
        (`…@lid`), a per-account id with nothing of the number in it — measured 23 Sep, every
        author Jeli holds in the groups is one. So a member typing their own number on Jeli's page
        cannot be found by it; WAHA is asked to translate, and the answer is kept for an hour.
        """
        digits = re.sub(r"\D", "", digits or "")
        if not digits:
            return []
        cached = self._lids.get(digits)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        found = [digits]
        # What WhatsApp has already told us about the groups' ids (learn_numbers), read backwards.
        found += [lid for lid, number in NUMBER_OF_LID.items() if number == digits]
        try:
            response = await self._http.get(f"/api/{self.session}/lids/pn/{digits}")
            if response.is_success:
                body = response.json()
                lid = _lid_in(body)
                if not lid:
                    # A 200 does not mean an id was found: WAHA answers with what it knows, and it
                    # knows nothing of a number whose groups it has not read (measured 23 Sep).
                    log.info("WhatsApp knows no account id for this number yet")
                if lid:
                    found.append(re.sub(r"\D", "", str(lid).split("@")[0]))
        except (httpx.HTTPError, ValueError):
            log.warning("Could not ask WAHA which id belongs to a number", exc_info=True)
        found = [item for item in dict.fromkeys(found) if item]
        self._lids[digits] = (time.monotonic(), found)
        return found

    async def group_admins(self, chat_id: str) -> set[str] | None:
        """The admins of a group (number or id digits), cached for an hour; None if unknown."""
        cached = self._admins.get(chat_id)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        try:
            response = await self._http.get(f"/api/{self.session}/groups/{chat_id}/participants")
            response.raise_for_status()
            participants = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        admins = set()
        for person in participants if isinstance(participants, list) else []:
            role = str(person.get("role", "")).lower()
            if role in ("admin", "superadmin") or person.get("isAdmin") or person.get("isSuperAdmin"):
                for key in ("id", "pn", "phoneNumber", "lid"):
                    if person.get(key):
                        admins.add(user_part(str(person[key])))
        self._admins[chat_id] = (time.monotonic(), admins)
        return admins

    async def aclose(self) -> None:
        await self._http.aclose()


def start(settings: Settings, respond: Respond, ingest: Ingest | None = None) -> Waha | None:
    if not settings.waha_url:
        log.info("WAHA_URL is not set: WhatsApp adapter disabled")
        return None
    missing = [
        name
        for name, value in (("WAHA_API_KEY", settings.waha_api_key), ("WAHA_WEBHOOK_HMAC_KEY", settings.waha_webhook_hmac_key))
        if not value
    ]
    if missing:
        raise RuntimeError(f"WAHA_URL is set but {', '.join(missing)} is missing")
    log.info("WhatsApp adapter enabled through WAHA at %s (session %s)", settings.waha_url, settings.waha_session)
    return Waha(settings, respond, ingest)


async def stop(adapter: Waha) -> None:
    await adapter.aclose()


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_webhook_hmac: Annotated[str | None, Header()] = None,
) -> dict:
    adapter: Waha | None = getattr(request.app.state, "whatsapp", None)
    body = await request.body()
    if adapter is None or not verify_signature(body, x_webhook_hmac, adapter.hmac_key):
        raise HTTPException(status_code=403)
    event = json.loads(body)
    if event.get("session") != adapter.session:
        return {"ok": True}
    if event.get("event") == "session.status":
        adapter.set_status((event.get("payload") or {}).get("status"))
        return {"ok": True}
    if event.get("event") == "poll.vote":
        payload = event.get("payload") or {}
        vote, poll = payload.get("vote") or {}, payload.get("poll") or {}
        if adapter.on_vote and poll.get("id") and vote.get("from"):
            background_tasks.add_task(adapter.on_vote, poll["id"], vote["from"], [str(o) for o in vote.get("selectedOptions") or []])
        return {"ok": True}
    if event.get("event") == "message.reaction":
        # Needs "message.reaction" in WAHA's WHATSAPP_HOOK_EVENTS; a 👍/👎 on Jeli's answer is feedback.
        background_tasks.add_task(adapter.reaction, event.get("payload") or {})
        return {"ok": True}

    shared = parse_shared_document(event)
    if shared and (not adapter.groups or shared["chat_id"] in adapter.groups):
        background_tasks.add_task(adapter.keep_document, shared)

    if event.get("me"):
        adapter._me = event["me"]  # kept for the messages read back from history, which carry none
    message = parse_message(event, adapter.bot_name)
    # Acknowledge at once and handle in the background, so WAHA never times out and retries.
    if message and adapter.accepts(message) and adapter.first_delivery(message.message_id):
        if adapter.ingest and message.text:  # a voice note is remembered once listened to
            background_tasks.add_task(adapter.ingest, message)
        background_tasks.add_task(adapter.handle, message)
    return {"ok": True}
