"""WhatsApp adapter through WAHA, a self-hosted WhatsApp Web gateway (unofficial).

WAHA keeps a WhatsApp session open for Jeli's dedicated number and forwards every message it
sees — in the group and in direct messages — to our webhook. Replies go back through WAHA's API.
The official Cloud API cannot read groups, which the challenge requires.

Because the channel is unofficial, the number can be restricted if it behaves like a machine.
Jeli therefore never starts a conversation, only answers when addressed, and paces itself
(see app/adapters/pacing.py).
"""

import asyncio
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
from app.config import Settings
from app.models import IncomingMessage

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


def parse_message(event: dict, bot_name: str) -> IncomingMessage | None:
    """Turn a WAHA `message` event into an IncomingMessage; None for anything Jeli should not process."""
    if event.get("event") != "message":
        return None
    payload = event.get("payload") or {}
    chat_id = payload.get("from") or ""
    text = (payload.get("body") or "").strip()
    if payload.get("fromMe") or not chat_id or chat_id.endswith(IGNORED_CHAT_SUFFIXES) or not text:
        return None

    me = event.get("me") or {}
    bot_ids = {user_part(me.get("id")), user_part(me.get("lid"))} - {""}
    is_private = not chat_id.endswith("@g.us")

    mentioned = bool(bot_ids & (_mentioned_ids(payload.get("_data")) | set(TEXT_MENTION.findall(text))))
    reply_to = payload.get("replyTo") or {}
    replied_to_bot = user_part(reply_to.get("participant")) in bot_ids
    named = re.match(rf"\s*{re.escape(bot_name)}\b[\s,:!?.-]*", text, flags=re.IGNORECASE) if bot_name else None
    command = text.startswith("/")

    for bot_id in bot_ids:
        text = text.replace(f"@{bot_id}", "")
    if named:
        text = text[named.end():]

    return IncomingMessage(
        platform="whatsapp",
        chat_id=chat_id,
        message_id=payload["id"],
        author=_author(payload),
        author_id=payload.get("participant") or chat_id,
        text="\n".join(" ".join(line.split()) for line in text.splitlines()).strip(),
        sent_at=datetime.fromtimestamp(int(float(payload["timestamp"])), tz=timezone.utc),
        is_private=is_private,
        addressed_to_bot=is_private or mentioned or replied_to_bot or bool(named) or command,
    )


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
        """Anti-ban guards: never answer while the session is unhealthy, late, or too often."""
        if self.paused:
            log.warning("Session is not WORKING: not answering message %s", message.message_id)
            return False
        age = (datetime.now(timezone.utc) - message.sent_at).total_seconds()
        if age > MAX_REPLY_AGE_SECONDS:
            log.info("Not answering message %s: %d s old (backlog after a reconnection)", message.message_id, age)
            return False
        if not self.user_limiter.allow(message.author_id or message.chat_id):
            log.warning("Member rate limit reached: not answering message %s", message.message_id)
            return False
        if not self.hourly_limiter.allow("all"):
            log.error("Hourly answer limit reached: Jeli stays silent until the window frees up")
            return False
        return True

    async def _post(self, path: str, payload: dict) -> None:
        response = await self._http.post(path, json={"session": self.session, **payload})
        if response.is_error:
            log.error("WAHA %s failed with %s: %s", path, response.status_code, response.text)
            if any(code in response.text for code in RESTRICTION_ERRORS):
                log.error(
                    "WhatsApp is temporarily restricting this number. Do NOT restart, log out or re-link "
                    "the session: the restriction lifts on its own."
                )
        response.raise_for_status()

    async def _post_quietly(self, path: str, payload: dict) -> None:
        """For cosmetic calls (read receipts, typing): a failure must not prevent the answer."""
        try:
            await self._post(path, payload)
        except httpx.HTTPError:
            log.warning("WAHA %s failed, continuing", path)

    async def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> None:
        payload = {"chatId": chat_id, "text": text, "linkPreview": False}
        if reply_to:
            payload["reply_to"] = reply_to
        await self._post("/api/sendText", payload)

    async def handle(self, message: IncomingMessage) -> None:
        try:
            if message.addressed_to_bot:
                await self._converse(message)
            else:
                await self._step_in_if_needed(message)
        except Exception:
            log.exception("Failed to handle WhatsApp message %s", message.message_id)

    async def _converse(self, message: IncomingMessage) -> None:
        """Answer like a person would: read, type for a while, then reply (WAHA's recommended sequence)."""
        if not self.may_reply(message):
            return
        chat = {"chatId": message.chat_id}
        await asyncio.sleep(reading_delay())
        await self._post_quietly("/api/sendSeen", {**chat, "messageIds": [message.message_id]})
        await self._post_quietly("/api/startTyping", chat)
        try:
            typing_since = time.monotonic()
            reply = await self.respond(message)
            if reply:
                # Answer generation counts as typing time: only wait for what is left.
                await asyncio.sleep(max(0.0, typing_duration(reply) - (time.monotonic() - typing_since)))
                # Still "typing…" while other answers go out first.
                await self.spacer.wait_turn()
        finally:
            await self._post_quietly("/api/stopTyping", chat)
        if reply:
            await self.send_text(message.chat_id, reply, reply_to=message.message_id)

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
        await self.send_text(message.chat_id, reply, reply_to=message.message_id)

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

    message = parse_message(event, adapter.bot_name)
    # Acknowledge at once and handle in the background, so WAHA never times out and retries.
    if message and adapter.accepts(message) and adapter.first_delivery(message.message_id):
        if adapter.ingest:
            background_tasks.add_task(adapter.ingest, message)
        background_tasks.add_task(adapter.handle, message)
    return {"ok": True}
