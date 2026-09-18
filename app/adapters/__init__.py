"""Chat platform adapters (Telegram, WhatsApp, ...).

An adapter is a thin, swappable layer: it turns platform updates into `IncomingMessage`,
calls `app.answer.responder.respond`, and sends the reply back. No business logic here.
"""
