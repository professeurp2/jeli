"""WhatsApp Cloud API adapter (Meta, official).

The Cloud API cannot read groups: members ask Jeli their questions in a direct message.
The group's content reaches the knowledge base through chat exports (and, optionally, Baileys).
"""

import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from app.answer.responder import respond
from app.config import Settings, get_settings
from app.models import IncomingMessage

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/whatsapp/webhook"
GRAPH_URL = "https://graph.facebook.com"
MAX_TEXT_LENGTH = 4096
SEEN_IDS_KEPT = 1000

router = APIRouter()


def verify_signature(body: bytes, signature_header: str | None, app_secret: str) -> bool:
    """Check Meta's X-Hub-Signature-256 header: HMAC-SHA256 of the raw body, keyed with the app secret."""
    if not signature_header or not app_secret:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature_header.encode())


def parse_messages(payload: dict) -> list[IncomingMessage]:
    """Extract incoming text messages from a webhook payload. Delivery statuses and other events are ignored."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name") for c in value.get("contacts", [])}
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    log.info("Ignoring WhatsApp message of type %s", message.get("type"))
                    continue
                sender = message["from"]
                messages.append(
                    IncomingMessage(
                        platform="whatsapp",
                        chat_id=sender,
                        message_id=message["id"],
                        author=names.get(sender) or "Someone",
                        text=message["text"]["body"].strip(),
                        sent_at=datetime.fromtimestamp(int(message["timestamp"]), tz=timezone.utc),
                        is_private=True,
                        addressed_to_bot=True,
                    )
                )
    return messages


class WhatsAppCloud:
    def __init__(self, settings: Settings):
        self.app_secret = settings.whatsapp_app_secret
        self._http = httpx.AsyncClient(
            base_url=f"{GRAPH_URL}/{settings.whatsapp_api_version}/{settings.whatsapp_phone_number_id}",
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            timeout=15,
        )
        # Meta may deliver the same webhook more than once: remember recent message ids.
        self._seen: OrderedDict[str, None] = OrderedDict()

    def first_delivery(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen[message_id] = None
        if len(self._seen) > SEEN_IDS_KEPT:
            self._seen.popitem(last=False)
        return True

    async def _post(self, payload: dict) -> None:
        response = await self._http.post("/messages", json=payload)
        if response.is_error:
            log.error("WhatsApp API error %s: %s", response.status_code, response.text)
        response.raise_for_status()

    async def mark_read_and_typing(self, message_id: str) -> None:
        await self._post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            }
        )

    async def send_text(self, to: str, body: str, reply_to: str | None = None) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body[:MAX_TEXT_LENGTH], "preview_url": False},
        }
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        await self._post(payload)

    async def handle(self, message: IncomingMessage) -> None:
        try:
            try:
                await self.mark_read_and_typing(message.message_id)
            except httpx.HTTPError:
                log.warning("Could not mark WhatsApp message %s as read", message.message_id)
            reply = await respond(message)
            if reply:
                await self.send_text(message.chat_id, reply, reply_to=message.message_id)
        except Exception:
            log.exception("Failed to answer WhatsApp message %s", message.message_id)

    async def aclose(self) -> None:
        await self._http.aclose()


def start(settings: Settings) -> WhatsAppCloud | None:
    required = {
        "WHATSAPP_ACCESS_TOKEN": settings.whatsapp_access_token,
        "WHATSAPP_PHONE_NUMBER_ID": settings.whatsapp_phone_number_id,
        "WHATSAPP_APP_SECRET": settings.whatsapp_app_secret,
        "WHATSAPP_VERIFY_TOKEN": settings.whatsapp_verify_token,
    }
    missing = [name for name, value in required.items() if not value]
    if len(missing) == len(required):
        log.info("WhatsApp Cloud adapter not configured")
        return None
    if missing:
        log.warning("WhatsApp Cloud adapter disabled, missing: %s", ", ".join(missing))
        return None
    log.info("WhatsApp Cloud adapter enabled on phone number id %s", settings.whatsapp_phone_number_id)
    return WhatsAppCloud(settings)


async def stop(adapter: WhatsAppCloud) -> None:
    await adapter.aclose()


@router.get(WEBHOOK_PATH, include_in_schema=False)
async def verify_webhook(
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> PlainTextResponse:
    """Meta calls this once when the webhook URL is registered in the app dashboard."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and verify_token and hmac.compare_digest(verify_token.encode(), expected.encode()):
        return PlainTextResponse(challenge or "")
    raise HTTPException(status_code=403)


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
) -> dict:
    adapter: WhatsAppCloud | None = getattr(request.app.state, "whatsapp", None)
    body = await request.body()
    if adapter is None or not verify_signature(body, x_hub_signature_256, adapter.app_secret):
        raise HTTPException(status_code=403)
    # Acknowledge at once and answer in the background: Meta retries webhooks that are slow to return 200.
    for message in parse_messages(json.loads(body)):
        if adapter.first_delivery(message.message_id):
            background_tasks.add_task(adapter.handle, message)
    return {"ok": True}
