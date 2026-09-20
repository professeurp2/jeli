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
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.adapters import Ingest, Respond
from app.adapters.pacing import SendSpacer, SlidingWindowLimiter, reading_delay, typing_duration
from app.answer.citations import is_ignored, poll_text
from app.answer.react import emotion_emoji, is_correction
from app.answer.voice import asks_for_voice, sources, spoken, without_voice_request
from app.config import Settings
from app.control.guard import Guard
from app.models import Attachment, IncomingMessage

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
# Documents Jeli keeps when a member shares them in a group.
DOCUMENT_TYPES = (".pdf", ".docx", ".txt", ".md")
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
# Shown as "recording audio…" for as long as a person would take to record the voice note, at most.
VOICE_RECORDING_SECONDS = 12

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
    if payload.get("fromMe") or not chat_id or chat_id.endswith(IGNORED_CHAT_SUFFIXES) or not (text or voice):
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
        self.spacer = SendSpacer(settings.whatsapp_min_send_interval_seconds)
        # Set from WAHA's session.status events: Jeli stays silent while the session is not WORKING.
        self.paused = False
        self.status: str | None = None
        # Set by the team from the dashboard: Jeli keeps remembering the groups but sends nothing.
        self.suspended = False
        # Groups where Jeli listens and ingests, but never replies (listen-only / silent mode).
        self.silent_groups: set[str] = set()
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
        # Whether this member was talking with Jeli a moment ago (set by the responder).
        self.in_conversation = None
        self._later: set[asyncio.Task] = set()
        self._admins: dict[str, tuple[float, set[str]]] = {}

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
            status = response.json().get("status")
        except (httpx.HTTPError, ValueError) as error:
            log.error("Cannot read the session status from WAHA at %s: %r", self._http.base_url, error)
            return
        self.set_status(status)

    def may_reply(self, message: IncomingMessage) -> bool:
        """Anti-ban guards: never answer while paused by the team or the session is unhealthy, nor
        late, too often, or to a member who misuses Jeli."""
        if message.chat_id in self.silent_groups:
            log.info("Group %s is in silent mode: Jeli listens but does not reply", message.chat_id)
            return False
        if self.suspended:
            log.info("Jeli is paused by the team: not answering message %s", message.message_id)
            return False
        if self.paused:
            log.warning("Session is not WORKING: not answering message %s", message.message_id)
            return False
        age = (datetime.now(timezone.utc) - message.sent_at).total_seconds()
        if age > MAX_REPLY_AGE_SECONDS:
            log.info("Not answering message %s: %d s old (backlog after a reconnection)", message.message_id, age)
            return False
        if self.guard:
            refusal = self.guard.check(message) if message.addressed_to_bot else (
                "blocked" if is_ignored(message, self.guard.blocked) else None
            )
            if refusal:
                log.info("Not answering message %s: %s", message.message_id, refusal)
                return False
        if not self.user_limiter.allow(message.author_id or message.chat_id):
            log.warning("Member rate limit reached: not answering message %s", message.message_id)
            if self.guard and message.addressed_to_bot:
                self.guard.report(message, "flood")
            return False
        if not self.hourly_limiter.allow("all"):
            log.error("Hourly answer limit reached: Jeli stays silent until the window frees up")
            return False
        return True

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
        return result.get("id")

    async def send_voice(self, chat_id: str, audio: bytes, reply_to: str | None = None) -> None:
        """A voice note: WAHA turns the audio (WAV) into the OGG/Opus that WhatsApp plays."""
        payload = {
            "chatId": chat_id,
            "file": {"mimetype": "audio/wav", "filename": "jeli.wav", "data": base64.b64encode(audio).decode()},
            "convert": True,
        }
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendVoice", payload)

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
        """React to a message with an emoji. Non-critical: never raises."""
        await self._post_quietly("/api/sendReaction", {
            "chatId": chat_id,
            "messageId": message_id,
            "reaction": emoji,
        })

    async def delete_message(self, chat_id: str, message_id: str) -> None:
        """Delete one of Jeli's own messages (e.g. a wrong answer). Non-critical: never raises."""
        await self._post_quietly("/api/deleteMessage", {
            "chatId": chat_id,
            "messageId": message_id,
        })

    async def send_reply(self, message: IncomingMessage, reply: str) -> None:
        """Jeli's reply, as WhatsApp shows it: quoting the member's message, or the source message
        itself (WhatsApp's own reference), with its mentions."""
        sent_id = await self.send_text(
            message.chat_id,
            reply,
            reply_to=getattr(reply, "reply_to", None) or message.message_id,
            mentions=getattr(reply, "mentions", ()),
        )
        # Track Jeli's own message so a subsequent correction can delete it.
        if sent_id:
            self._last_sent[message.chat_id] = sent_id

    async def handle(self, message: IncomingMessage) -> None:
        if self.suspended:
            return  # the message is still remembered (ingested separately)
        try:
            if message.voice_url:
                message = await self._listen(message)
                if message is None:
                    return
            if not message.addressed_to_bot and self.follow_up and self.follow_up(message):
                message = dataclasses.replace(message, addressed_to_bot=True)
            if message.addressed_to_bot:
                await self._converse(message)
            else:
                await self._step_in_if_needed(message)
                # Emotional reaction for group messages not addressed to Jeli (and not silent groups).
                if (not message.is_private and not self.paused
                        and message.chat_id not in self.silent_groups):
                    emoji = emotion_emoji(message.text)
                    if emoji:
                        await self.send_reaction(message.chat_id, message.message_id, emoji)
        except Exception:
            log.exception("Failed to handle WhatsApp message %s", message.message_id)

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
        if not heard:
            return None
        message = dataclasses.replace(message, text=heard, voice_url=None, reply_by_voice=True)
        if self.ingest and not message.is_private:
            await self.ingest(dataclasses.replace(message, text=f"🎤 {heard}"))  # the group's memory keeps it too
        return message

    async def _converse(self, message: IncomingMessage) -> None:
        """Answer like a person would: read, type (or record) for a while, then reply (WAHA's
        recommended sequence). Asked by voice, or asked for a voice reply: a voice note."""
        if not self.may_reply(message):
            return
        # When someone corrects Jeli, acknowledge immediately and delete the wrong message.
        if is_correction(message.text) and message.chat_id in self._last_sent:
            wrong_id = self._last_sent.pop(message.chat_id)
            await self.send_reaction(message.chat_id, message.message_id, "🙏")
            await self.delete_message(message.chat_id, wrong_id)
            log.info("Deleted Jeli's wrong message %s after correction in %s", wrong_id, message.chat_id)
        chat = {"chatId": message.chat_id}
        by_voice = self.voice is not None and message.reply_by_voice
        if by_voice:
            message = dataclasses.replace(message, text=without_voice_request(message.text))
        await asyncio.sleep(reading_delay())
        await self._post_quietly("/api/sendSeen", {**chat, "messageIds": [message.message_id]})
        await self._post_quietly("/api/startTyping", chat)
        audio = None
        try:
            typing_since = time.monotonic()
            reply = await self.respond(message)
            if reply and by_voice:
                await self._post_quietly(f"/api/{self.session}/presence", {**chat, "presence": "recording"})
                audio = await self.voice.speak(spoken(reply))
            if reply:
                # Answer generation counts as typing time: only wait for what is left.
                busy = min(VOICE_RECORDING_SECONDS, len(audio) / 48_000) if audio else typing_duration(reply)
                await asyncio.sleep(max(0.0, busy - (time.monotonic() - typing_since)))
                # Still "typing…" while other answers go out first.
                await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        if reply:
            if not (audio and await self._send_voice_reply(message, reply, audio)):
                await self.send_reply(message, reply)
            await self.deliver_files(message, reply)

    async def _send_voice_reply(self, message: IncomingMessage, reply: str, audio: bytes) -> bool:
        """The answer as a voice note replying to the member, then its sources in writing (a voice
        note cannot quote). False when the voice note could not be sent: the text goes instead."""
        try:
            await self.send_voice(message.chat_id, audio, reply_to=message.message_id)
        except httpx.HTTPError:
            log.warning("Voice note for message %s not sent: answering in writing", message.message_id)
            return False
        written = sources(reply)
        source_message = getattr(reply, "reply_to", None)
        if written or source_message:
            await self.spacer.wait_turn()
            # Said in this chat: point at the message itself, as WhatsApp does.
            await self.send_text(message.chat_id, written or "📌", reply_to=source_message or message.message_id)
        return True

    async def _step_in_if_needed(self, message: IncomingMessage) -> None:
        """A message not addressed to Jeli: it speaks only when the responder finds that the group
        already answered this question (R7), and within the same anti-ban limits."""
        reply = await self.respond(message)
        if not reply or not self.may_reply(message):
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

    shared = parse_shared_document(event)
    if shared and (not adapter.groups or shared["chat_id"] in adapter.groups):
        background_tasks.add_task(adapter.keep_document, shared)

    message = parse_message(event, adapter.bot_name)
    # Acknowledge at once and handle in the background, so WAHA never times out and retries.
    if message and adapter.accepts(message) and adapter.first_delivery(message.message_id):
        if adapter.ingest and message.text:  # a voice note is remembered once listened to
            background_tasks.add_task(adapter.ingest, message)
        background_tasks.add_task(adapter.handle, message)
    return {"ok": True}
