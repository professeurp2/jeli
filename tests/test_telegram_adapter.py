from datetime import datetime, timezone

from telegram import Chat, Message, User
from telegram.constants import ChatType

from app.adapters.telegram import to_incoming

BOT = User(id=42, first_name="Jeli", is_bot=True, username="jeli_bot")
AWA = User(id=1, first_name="Awa", last_name="Traoré", is_bot=False)
GROUP = Chat(id=-1001234567890, type=ChatType.SUPERGROUP, title="Cohort 1")
PRIVATE = Chat(id=1, type=ChatType.PRIVATE)
SENT_AT = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


def make_message(text, chat=GROUP, reply_to=None, from_user=AWA, message_id=7):
    return Message(
        message_id=message_id,
        date=SENT_AT,
        chat=chat,
        from_user=from_user,
        text=text,
        reply_to_message=reply_to,
    )


def test_plain_group_message_is_not_addressed_to_bot():
    incoming = to_incoming(make_message("Is the bootcamp still on Monday?"), BOT.id, BOT.username)
    assert not incoming.addressed_to_bot
    assert incoming.author == "Awa Traoré"
    assert incoming.chat_id == str(GROUP.id)


def test_mention_addresses_bot_and_is_stripped():
    incoming = to_incoming(make_message("@Jeli_Bot what was decided?"), BOT.id, BOT.username)
    assert incoming.addressed_to_bot
    assert incoming.text == "what was decided?"


def test_reply_to_bot_addresses_bot():
    bot_message = make_message("Hello!", from_user=BOT, message_id=6)
    incoming = to_incoming(make_message("and the deadline?", reply_to=bot_message), BOT.id, BOT.username)
    assert incoming.addressed_to_bot


def test_direct_message_addresses_bot():
    incoming = to_incoming(make_message("hi", chat=PRIVATE), BOT.id, BOT.username)
    assert incoming.addressed_to_bot
    assert incoming.is_private
    assert incoming.link is None


def test_supergroup_message_has_a_link():
    incoming = to_incoming(make_message("hi"), BOT.id, BOT.username)
    assert incoming.link == "https://t.me/c/1234567890/7"
