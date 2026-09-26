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


# How many numbers are asked of WhatsApp at once when they are not already known. A group of two
# hundred and forty is two hundred and forty questions; asked one after another that is minutes of
# waiting, and all at once it is a burst WAHA has no reason to enjoy.
AT_A_TIME = 8


async def _number_of(adapter, person: dict) -> str:
    """The phone number of one participant, or "" when WhatsApp will not say.

    WhatsApp gives a group's people by account id, and the number is either alongside it, already
    known (app/answer/citations.py), or has to be asked for. Never guessed: a wrong number here is
    a stranger receiving the team's campaign.
    """
    for key in ("pn", "phoneNumber"):
        number = a_number(str(person.get(key) or "").split("@")[0])
        if number:
            return number
    raw = str(person.get("id") or person.get("lid") or "")
    if not raw:
        return ""
    try:
        return a_number(await adapter.number_behind(raw))
    except Exception:
        log.warning("Could not ask WhatsApp who an account id belongs to", exc_info=True)
        return ""


def _keys(person: dict) -> set[str]:
    """Every digit-string this participant answers to, for matching against the admins."""
    out = set()
    for key in ("id", "pn", "phoneNumber", "lid"):
        raw = str(person.get(key) or "")
        if raw:
            out.add(re.sub(r"\D", "", raw.split("@")[0].split(":")[0]))
    return {k for k in out if k}


async def who_to_write_to(adapter, group_id: str, already: set[str]) -> tuple[list[str], dict]:
    """The numbers left to write to in this group, and a count of everyone left out and why."""
    people = await adapter.people_in(group_id)
    admins = await adapter.group_admins(group_id) or set()
    team = {re.sub(r"\D", "", n) for n in getattr(adapter, "admin_numbers", []) if n}
    left_out = {"group admins": 0, "the team": 0, "number unknown": 0, "already written to": 0}

    wanted = []
    for person in people:
        keys = _keys(person)
        if keys & admins:
            left_out["group admins"] += 1
        elif keys & team:
            left_out["the team"] += 1
        else:
            wanted.append(person)

    gate = asyncio.Semaphore(AT_A_TIME)

    async def ask(person):
        async with gate:
            return await _number_of(adapter, person)

    numbers: list[str] = []
    for number in await asyncio.gather(*(ask(p) for p in wanted)):
        if not number:
            left_out["number unknown"] += 1
        elif number in team:
            left_out["the team"] += 1
        elif number in already:
            left_out["already written to"] += 1
        elif number not in numbers:
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

    sent = await _write_to(adapter, store, voice, numbers[:limit] if limit else numbers, already, actor)
    remaining = len(numbers) - sent
    log.info("Campaign: wrote to %d members, %d still to reach", sent, remaining)
    if not sent:
        return "nothing was sent — Jeli is paused, or its hourly limit is reached"
    return f"written to {sent}, {remaining} still to reach"


async def send_to_numbers(adapter, store, voice, given: str, actor: str = "the team") -> str:
    """Write to the numbers the team typed in themselves, one per line. Returns what happened.

    WhatsApp will not always say which number an account id belongs to, and then Jeli cannot reach
    that member on its own. This is the way round it: the team reads the number off their phone and
    hands it over. It is the same message, the same voice note, and the same list of people already
    written to — so a number reached here is never written to again by the group button, and the
    other way round.

    A number typed here is a deliberate choice, so the team's own numbers are NOT skipped: the
    group button leaves them out because nobody campaigns to themselves, but typing one in is how
    the team tests what a member receives. Measured 26 September: the first test, with the tester's
    own number, sent nothing and said everyone had been written to already — neither was true.
    """
    kept = await store.load_settings() if store else {}
    already = {a_number(n) for n in (kept.get(CAMPAIGN) or []) if a_number(n)}

    wanted, bad, done, seen = [], 0, 0, set()
    # One per line: a number is copied off a phone with its spaces in it (+223 60 55 77 61), so
    # only a line break, a comma or a semicolon separates two people.
    for line in re.split(r"[\n\r,;]+", given or ""):
        if not line.strip():
            continue
        number = a_number(line)
        if not number:
            bad += 1
        elif number in seen:
            continue
        elif number in already:
            seen.add(number)
            done += 1
        else:
            seen.add(number)
            wanted.append(number)

    trouble = []
    if bad:
        trouble.append(f"{bad} did not look like a phone number")
    if done:
        trouble.append(f"{done} already written to")
    if not wanted:
        said = "; ".join(trouble) or "there was nothing in the box"
        return f"nothing to send ({said})"

    sent = await _write_to(adapter, store, voice, wanted, already, actor)
    if not sent:
        return "nothing was sent — Jeli is paused, or its hourly limit is reached"
    said = f"written to {sent}"
    if len(wanted) > sent:
        said += f", {len(wanted) - sent} could not be sent yet"
    if trouble:
        said += f" ({'; '.join(trouble)})"
    return said


async def _write_to(adapter, store, voice, numbers: list[str], already: set[str], actor: str) -> int:
    """Send the message and its voice note to each number in turn. Returns how many were reached.

    The voice note is made once and the same audio goes to everyone: one voice note's worth of
    quota for the whole run. Every number reached is written down before the next one is tried, so
    a crash, a deploy or a closed browser never costs someone a second copy.
    """
    spoken = None
    if voice is not None:
        try:
            spoken = await voice.speak(CAMPAIGN_SPOKEN, language="en")
        except Exception:
            log.warning("The campaign's voice note could not be made: sending the text alone", exc_info=True)
    sent = 0
    for number in numbers:
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
    return sent


def _in_words(left_out: dict) -> str:
    said = [f"{n} {why}" for why, n in left_out.items() if n]
    return "; ".join(said) if said else "nobody left out"
