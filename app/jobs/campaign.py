"""The one message the team asks Jeli to carry to each member privately: their request for a vote.

Everywhere else Jeli answers; the hello and the goodbye it posts once, to the group. This is the
third and last thing it sends unasked, and it is the only one that reaches people one by one. That
is why it is built the way it is:

- **Nobody is written to twice.** Every number reached is kept, so a second press continues where
  the first stopped instead of starting again. A deploy in the middle costs nothing.
- **It goes at Jeli's ordinary pace**, through the same hourly limit as every other message. Two
  hundred and forty private messages sent quickly is the surest way to have a WhatsApp number
  blocked, and this number carries the whole community.
- **The group's admins are left out**, as the team asked, and so are the team's own numbers.
- **It stops the moment Jeli is paused**, like everything else.

The voice note says the heart of it in a few sentences rather than reading the whole page aloud:
two hundred and fifty words read out is a minute and a half, which nobody listens to. It is made
once and the same audio is sent to everyone — one voice note's worth of quota for the whole run.
"""

import asyncio
import logging
import re

from app.answer.citations import NUMBER_OF_LID, a_number

log = logging.getLogger(__name__)

# The numbers already written to, kept in the settings so a restart never writes to anyone twice.
CAMPAIGN = "campaign.vote"

CAMPAIGN_TEXT = """\
Hello 👋

It's *Jeli* — the assistant you've been talking to these past days. What follows isn't mine. \
It's from the people who built me (*+223 60 55 77 61*), in their own words. They asked me to \
bring it to you myself.

—

Dear Innovators,

Building Jeli took a lot out of us. We worked through sleepless nights, and when the credits ran \
out we paid for more from our own pockets — not to win something, but because we wanted you to \
have an assistant that actually works.

Yes, we'd love to win. But what we want more is for Jeli to become the buddy and the assistant \
you can genuinely rely on.

That's why we taught it to:

- Talk back with its voice, not only in writing.
- React with stickers and emojis.
- Hold a real conversation, not a scripted one.
- Message you privately with reminders for classes and deadlines.
- Summarise a class you missed, or a whole PDF.
- Speak your language — in writing and out loud.
- Catch you up on everything said while you were away.
- And help with whatever else you need.

A vote for Jeli is a vote for the nights that went into it, and for a team that isn't finished \
working on it.

We'd be truly grateful for yours.

Thank you ❤️
"""

# What the voice note says. The written message stays whole; this is what a person will actually
# listen to — the same words, in the time someone gives a voice note from a bot.
CAMPAIGN_SPOKEN = (
    "Hello, it's Jeli. This message isn't mine — it's from the team who built me, and they asked "
    "me to bring it to you. They say: building Jeli took sleepless nights, and our own money when "
    "the credits ran out, because we wanted you to have something you could really rely on. An "
    "assistant that talks back with its voice, remembers your deadlines, catches you up on what "
    "you missed, and speaks your language. We would be truly grateful for your vote. Thank you."
)


def _number_of(person: dict) -> str:
    """The phone number of one participant as WAHA lists them, or "" when it cannot be known.

    WhatsApp gives a group's people by account id; the number is either alongside it or in what
    WhatsApp has already told us (app/answer/citations.py). Never guessed: a wrong number here is
    a stranger receiving the team's campaign.
    """
    for key in ("pn", "phoneNumber"):
        number = a_number(str(person.get(key) or "").split("@")[0])
        if number:
            return number
    raw = str(person.get("id") or person.get("lid") or "")
    if raw.endswith("@c.us"):
        return a_number(raw.split("@")[0])
    behind = NUMBER_OF_LID.get(re.sub(r"\D", "", raw.split("@")[0].split(":")[0]))
    return a_number(behind or "")


def _keys(person: dict) -> set[str]:
    """Every digit-string this participant answers to, for matching against the admins."""
    out = set()
    for key in ("id", "pn", "phoneNumber", "lid"):
        raw = str(person.get(key) or "")
        if raw:
            out.add(re.sub(r"\D", "", raw.split("@")[0].split(":")[0]))
    return {k for k in out if k}


async def who_to_write_to(adapter, group_id: str, already: set[str]) -> tuple[list[str], dict]:
    """The numbers left to write to, and a count of everyone left out and why."""
    people = await adapter.people_in(group_id)
    admins = await adapter.group_admins(group_id) or set()
    team = {re.sub(r"\D", "", n) for n in getattr(adapter, "admin_numbers", []) if n}
    left_out = {"group admins": 0, "the team": 0, "number unknown": 0, "already written to": 0}
    numbers: list[str] = []
    for person in people:
        keys = _keys(person)
        if keys & admins:
            left_out["group admins"] += 1
            continue
        number = _number_of(person)
        if not number:
            left_out["number unknown"] += 1
            continue
        if number in team or keys & team:
            left_out["the team"] += 1
            continue
        if number in already:
            left_out["already written to"] += 1
            continue
        if number not in numbers:
            numbers.append(number)
    return numbers, left_out


async def send_campaign(adapter, store, voice, group_id: str, actor: str = "the team",
                        limit: int | None = None) -> str:
    """Write to the next `limit` members still to reach. Returns what happened, for the team.

    The team commands each send: one press, one message — or a handful, when they say so. Nothing
    runs on its own, because this is the only thing Jeli sends to people who did not ask, and the
    person pressing should be able to read the first one before the second goes.

    Stops early if the hourly limit says no more; every number reached is written down before the
    next one is tried, so nothing is ever sent twice.
    """
    kept = await store.load_settings() if store else {}
    already = {a_number(n) for n in (kept.get(CAMPAIGN) or []) if a_number(n)}
    numbers, left_out = await who_to_write_to(adapter, group_id, already)
    if not numbers:
        return f"nobody left to write to ({_in_words(left_out)})"

    spoken = None
    if voice is not None:
        try:
            spoken = await voice.speak(CAMPAIGN_SPOKEN, language="en")
        except Exception:
            log.warning("The campaign's voice note could not be made: sending the text alone", exc_info=True)
    sent = 0
    for number in numbers[:limit] if limit else numbers:
        if adapter.suspended or adapter.paused:
            break
        if not await adapter.post(f"{number}@c.us", CAMPAIGN_TEXT.strip()):
            break  # the hourly limit, or WhatsApp is not connected: the rest waits for next time
        if spoken:
            try:
                await adapter.spacer.wait_turn()
                await adapter.send_voice(f"{number}@c.us", spoken)
            except Exception:
                log.warning("Could not send the campaign's voice note", exc_info=True)
        already.add(number)
        sent += 1
        if store:  # written down before the next one, so a crash never costs a second message
            await store.save_settings({CAMPAIGN: sorted(already)}, actor)
    if store and sent:
        await store.add_audit(actor, f"Sent the vote message to {sent} member{'s' if sent > 1 else ''}")
    remaining = len(numbers) - sent
    log.info("Campaign: wrote to %d members, %d still to reach", sent, remaining)
    if not sent:
        return "nothing was sent — Jeli is paused, or its hourly limit is reached"
    return f"written to {sent}, {remaining} still to reach"


def _in_words(left_out: dict) -> str:
    said = [f"{n} {why}" for why, n in left_out.items() if n]
    return "; ".join(said) if said else "nobody left out"
