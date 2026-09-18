import asyncio
from datetime import datetime, timezone

from app.answer.responder import respond
from app.models import IncomingMessage


def make_incoming(text, addressed_to_bot):
    return IncomingMessage(
        platform="telegram",
        chat_id="-100123",
        message_id="1",
        author="Awa",
        text=text,
        sent_at=datetime(2026, 9, 18, tzinfo=timezone.utc),
        is_private=False,
        addressed_to_bot=addressed_to_bot,
    )


def test_stays_silent_when_not_addressed():
    assert asyncio.run(respond(make_incoming("random chat", addressed_to_bot=False))) is None


def test_replies_when_addressed():
    reply = asyncio.run(respond(make_incoming("hello", addressed_to_bot=True)))
    assert reply is not None
    assert "Awa" in reply


def test_greets_on_bare_mention():
    reply = asyncio.run(respond(make_incoming("", addressed_to_bot=True)))
    assert reply is not None
    assert "Jeli" in reply
