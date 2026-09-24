"""The two messages Jeli sends of its own accord in the cohort group: hello, and goodbye.

Everywhere else Jeli only ever answers. These two are the exception, and the reason is simple: a
bot that appears in a 240-member group without a word is a stranger reading over their shoulder,
and one that vanishes after the judging without a word is a service that broke. Both are said once,
by a person on the team pressing a button, and never again — the group's own conversation is not
Jeli's to fill.

Each is written once and kept, so the team can read it, change a word and see exactly what will be
posted before anyone does.
"""

import logging

log = logging.getLogger(__name__)

# Sent once each. The key is remembered in the settings, so a second press does nothing.
HELLO = "greeting.hello"
GOODBYE = "greeting.goodbye"

HELLO_TEXT = """\
Hello everyone 👋

I'm *Jeli*, the griot of this group. Just as the griots of old kept their community's memory, I \
keep yours: the announcements, the decisions, the recorded sessions, the deadlines. Nothing gets \
lost.

*What you can ask me*
• _"When is the next session?"_ — I answer, and I say who announced it
• _"What did I miss since Monday?"_ — I'll catch you up
• _"Summarise yesterday's session"_ — I listen to the recordings
• _"Send me the Information Pack"_ — I'll send the document, translated if you ask
• _"Remind me before the meeting"_ — I'll tell you in time, here or privately
• _/deadlines_ — everything due in the next two weeks

Write to me in English, en français, kwa Kiswahili, በአማርኛ — I answer in your language. In writing \
or by voice, whichever suits you: I can listen to a voice note and reply with one, in Swahili, \
Amharic, Hausa, Yoruba, Kinyarwanda and others. You can even *call me and talk to me out loud* — \
ask me for "the call link" and I'll send it 📞

*Two things that matter.* I only speak when spoken to: start your message with "Jeli", or reply to \
one of mine. And I never say anything nobody said here — when I don't know, I say so.

Good luck to every team 🙏
"""

GOODBYE_TEXT = """\
One last word, and I'll leave you to it 🙏

The testing is over. Thank you, each of you: you asked me questions I couldn't answer, and that is \
how I learned. Every "you got that wrong" of these past days corrected something.

What you built together stays: the sessions, the decisions, the deadlines — all of it is kept.

I'm going quiet now. If the team ever wakes me up again, I'll remember everything.

Safe travels to every team. It has been an honour to keep your memory ✨
"""


async def send_greeting(adapter, store, which: str, chat_id: str, actor: str = "the team") -> str:
    """Post the hello or the goodbye in one group, once. Returns what happened, for the team."""
    text = HELLO_TEXT if which == HELLO else GOODBYE_TEXT
    already = await store.load_settings() if store else {}
    if already.get(which):
        return "already sent"
    if not chat_id:
        return "no group"
    await adapter.send_text(chat_id, text.strip())
    if store:
        await store.save_settings({which: chat_id}, actor)
        await store.add_audit(actor, f"Sent the {'hello' if which == HELLO else 'goodbye'} message to the group")
    log.info("Sent the %s message to %s", which, chat_id)
    return "sent"
