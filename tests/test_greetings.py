"""Jeli's hello and goodbye: posted once each, by the team, never twice."""

import asyncio

from app.jobs.greetings import GOODBYE, GOODBYE_TEXT, HELLO, HELLO_TEXT, send_greeting


class Channel:
    def __init__(self):
        self.sent = []

    async def send_text(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class Settings:
    def __init__(self, saved=None):
        self.saved = dict(saved or {})
        self.audit = []

    async def load_settings(self):
        return dict(self.saved)

    async def save_settings(self, values, actor):
        self.saved.update(values)

    async def add_audit(self, actor, action):
        self.audit.append((actor, action))


GROUP = "120363000000000000@g.us"


def test_the_hello_is_posted_once_and_never_again():
    channel, store = Channel(), Settings()
    assert asyncio.run(send_greeting(channel, store, HELLO, GROUP, "stanley")) == "sent"
    assert channel.sent == [(GROUP, HELLO_TEXT.strip())]
    assert store.saved[HELLO] == GROUP and store.audit
    # A second press does nothing: the group's conversation is not Jeli's to fill.
    assert asyncio.run(send_greeting(channel, store, HELLO, GROUP, "stanley")) == "already sent"
    assert len(channel.sent) == 1


def test_the_goodbye_is_its_own_message():
    channel, store = Channel(), Settings({HELLO: GROUP})
    assert asyncio.run(send_greeting(channel, store, GOODBYE, GROUP, "stanley")) == "sent"
    assert channel.sent[0][1] == GOODBYE_TEXT.strip()


def test_without_a_group_nothing_is_posted():
    channel = Channel()
    assert asyncio.run(send_greeting(channel, Settings(), HELLO, "", "stanley")) == "no group"
    assert channel.sent == []


def test_both_messages_say_what_jeli_is_and_what_it_will_not_do():
    """In English: it is the programme's common language, and what the team greets Jeli in."""
    # The hello must set expectations: Jeli speaks when spoken to, and says when it does not know.
    assert "Jeli" in HELLO_TEXT and "voice note" in HELLO_TEXT
    assert "when I don't know, I say so" in HELLO_TEXT
    assert "I only speak when spoken to" in HELLO_TEXT
    assert "/deadlines" in HELLO_TEXT
    # And it says, in their own languages, that it answers in them.
    for language in ("en français", "kwa Kiswahili", "Amharic", "Hausa"):
        assert language in HELLO_TEXT, language
    # The goodbye must not promise a service that is stopping.
    assert "going quiet" in GOODBYE_TEXT and "Thank you" in GOODBYE_TEXT
    assert "all of it is kept" in GOODBYE_TEXT
