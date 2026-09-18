"""Chat platform adapters (Telegram, WhatsApp, ...).

An adapter is a thin, swappable layer: it turns platform updates into `IncomingMessage`,
calls the `respond` function it is given (Responder.respond), and sends the reply back.
No business logic here.
"""

from collections.abc import Awaitable, Callable

from app.models import IncomingMessage

Respond = Callable[[IncomingMessage], Awaitable[str | None]]
Ingest = Callable[[IncomingMessage], Awaitable[None]]
