"""Telegram adapter: webhook mode when deployed, polling mode for local development."""

import logging
import re
import secrets
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request
from telegram import Message, Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from app.adapters import Respond
from app.config import Settings, get_settings
from app.models import IncomingMessage

log = logging.getLogger(__name__)

WEBHOOK_PATH = "/telegram/webhook"

HELP_TEXT = (
    "Hi, I'm Jeli, the group's memory.\n\n"
    "Mention me (@{username}) in the group, reply to one of my messages, or write to me "
    "directly with a question about anything discussed in the chats or calls. "
    "I'll answer with my sources."
)

router = APIRouter()


def to_incoming(message: Message, bot_id: int, bot_username: str) -> IncomingMessage:
    mention = f"@{bot_username}"
    text = message.text or ""
    reply_to = message.reply_to_message
    is_private = message.chat.type == ChatType.PRIVATE
    mentioned = mention.lower() in text.lower()
    replied_to_bot = bool(reply_to and reply_to.from_user and reply_to.from_user.id == bot_id)
    return IncomingMessage(
        platform="telegram",
        chat_id=str(message.chat.id),
        message_id=str(message.message_id),
        author=message.from_user.full_name if message.from_user else "Someone",
        author_id=str(message.from_user.id) if message.from_user else None,
        text=re.sub(re.escape(mention), "", text, flags=re.IGNORECASE).strip(),
        sent_at=message.date,
        is_private=is_private,
        addressed_to_bot=is_private or mentioned or replied_to_bot,
        link=message.link,
    )


async def _on_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT.format(username=context.bot.username))


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    respond: Respond = context.bot_data["respond"]
    reply = await respond(to_incoming(message, context.bot.id, context.bot.username))
    if reply:
        await message.reply_text(reply)
        attachment = getattr(reply, "attachment", None)
        pending = getattr(reply, "pending", None)
        if pending:
            attachment = await pending()
            if isinstance(attachment, str):
                await message.reply_text(attachment)
                attachment = None
        if attachment:
            await message.reply_document(attachment.data, filename=attachment.filename, caption=attachment.caption)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error while handling a Telegram update", exc_info=context.error)


def build_application(token: str, respond: Respond) -> Application:
    application = Application.builder().token(token).build()
    application.bot_data["respond"] = respond
    application.add_handler(CommandHandler(["start", "help"], _on_help))
    application.add_handler(
        # New messages only: an edited message must not trigger a second reply.
        MessageHandler(filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, _on_text)
    )
    application.add_error_handler(_on_error)
    return application


async def start(settings: Settings, respond: Respond) -> Application | None:
    if not settings.telegram_bot_token:
        log.warning("TELEGRAM_BOT_TOKEN is not set: Telegram adapter disabled")
        return None
    if settings.public_url and not settings.telegram_webhook_secret:
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET is required when PUBLIC_URL is set")

    application = build_application(settings.telegram_bot_token, respond)
    await application.initialize()
    await application.start()
    if settings.public_url:
        await application.bot.set_webhook(
            url=settings.public_url.rstrip("/") + WEBHOOK_PATH,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        log.info("Telegram adapter running in webhook mode as @%s", application.bot.username)
    else:
        await application.bot.delete_webhook()
        await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        log.info("Telegram adapter running in polling mode as @%s", application.bot.username)
    return application


async def stop(application: Application) -> None:
    if application.updater and application.updater.running:
        await application.updater.stop()
    await application.stop()
    await application.shutdown()


@router.post(WEBHOOK_PATH, include_in_schema=False)
async def webhook(
    request: Request,
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> dict:
    application: Application | None = getattr(request.app.state, "telegram", None)
    secret = get_settings().telegram_webhook_secret
    received = x_telegram_bot_api_secret_token or ""
    if application is None or not secret or not secrets.compare_digest(received.encode(), secret.encode()):
        raise HTTPException(status_code=403)
    # Queue the update and return at once: Telegram does not wait for the answer to be generated.
    await application.update_queue.put(Update.de_json(await request.json(), application.bot))
    return {"ok": True}
