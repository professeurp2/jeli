"""The vote message: the one thing Jeli sends to members one by one.

Two hundred and forty private messages is both the most useful thing the team can ask of Jeli and
the surest way to lose its number. These tests hold the rails.
"""

import asyncio
import re

from app.jobs.campaign import CAMPAIGN, CAMPAIGN_SPOKEN, CAMPAIGN_TEXT, send_campaign, who_to_write_to


class Waha:
    def __init__(self, people, admins=(), team=(), allowed=99, behind=None):
        self.people, self.admins = people, set(admins)
        self.admin_numbers = list(team)
        self.suspended = self.paused = False
        self.sent, self.voices, self.allowed = [], [], allowed
        self.spacer = self
        # What WhatsApp answers when asked who an account id belongs to.
        self.behind, self.asked = dict(behind or {}), []

    async def number_behind(self, account_id):
        digits = re.sub(r"\D", "", str(account_id).split("@")[0].split(":")[0])
        self.asked.append(digits)
        if str(account_id).endswith("@c.us"):
            return digits
        return self.behind.get(digits, "")

    async def wait_turn(self):
        return None

    async def people_in(self, chat_id):
        return self.people

    async def group_admins(self, chat_id):
        return self.admins

    async def post(self, chat_id, text):
        if len(self.sent) >= self.allowed:
            return False  # the hourly limit: the rest waits for the next press
        self.sent.append((chat_id, text))
        return True

    async def send_voice(self, chat_id, audio):
        self.voices.append((chat_id, audio))


class Store:
    def __init__(self, kept=None):
        self.kept, self.audit = dict(kept or {}), []

    async def load_settings(self):
        return dict(self.kept)

    async def save_settings(self, values, actor):
        self.kept.update(values)

    async def add_audit(self, actor, what):
        self.audit.append((actor, what))


class Voice:
    def __init__(self):
        self.asked = []

    async def speak(self, text, language=""):
        self.asked.append((text, language))
        return b"OggS-audio"


MEMBERS = [
    {"id": "22370000001@c.us"},
    {"id": "1111@lid", "pn": "22370000002@c.us"},
    {"id": "22370000003@c.us"},
]
BOSS = {"id": "22399999999@c.us"}


def test_the_groups_admins_and_the_team_are_never_written_to():
    waha = Waha([*MEMBERS, BOSS], admins={"22399999999"}, team=["22370000003"])
    numbers, why = asyncio.run(who_to_write_to(waha, "g@g.us", already=set()))
    assert numbers == ["22370000001", "22370000002"]
    assert why["group admins"] == 1 and why["the team"] == 1


def test_a_member_whose_number_nobody_knows_is_left_out_rather_than_guessed():
    """A wrong number here is a stranger receiving the team's campaign."""
    waha = Waha([{"id": "5555@lid"}, {"id": "22370000001@c.us"}])
    numbers, why = asyncio.run(who_to_write_to(waha, "g@g.us", already=set()))
    assert numbers == ["22370000001"] and why["number unknown"] == 1


def test_nobody_is_ever_written_to_twice():
    store = Store({CAMPAIGN: ["22370000001"]})
    waha = Waha(MEMBERS)
    said = asyncio.run(send_campaign(waha, store, Voice(), "g@g.us", actor="stanley"))
    written = [chat for chat, _ in waha.sent]
    assert written == ["22370000002@c.us", "22370000003@c.us"]
    assert said == "written to 2, 0 still to reach"  # the first was skipped, not rewritten
    # And the record grows, so the next press skips these too.
    assert set(store.kept[CAMPAIGN]) == {"22370000001", "22370000002", "22370000003"}


def test_each_member_gets_the_words_and_the_voice_and_the_voice_is_made_once():
    store, voice, waha = Store(), Voice(), Waha(MEMBERS)
    asyncio.run(send_campaign(waha, store, voice, "g@g.us"))
    assert len(waha.sent) == 3 and len(waha.voices) == 3
    assert all(text == CAMPAIGN_TEXT.strip() for _, text in waha.sent)
    # One synthesis for the whole run, in English, and the short version — not the whole page.
    assert voice.asked == [(CAMPAIGN_SPOKEN, "en")]
    assert len(CAMPAIGN_SPOKEN) < len(CAMPAIGN_TEXT) / 2


def test_it_stops_at_the_hourly_limit_and_continues_on_the_next_press():
    """Sending faster than Jeli's ordinary pace is how a WhatsApp number gets blocked."""
    store, waha = Store(), Waha(MEMBERS, allowed=2)
    first = asyncio.run(send_campaign(waha, store, None, "g@g.us"))
    assert len(waha.sent) == 2 and "1 still to reach" in first
    waha.allowed = 99
    asyncio.run(send_campaign(waha, store, None, "g@g.us"))
    assert [chat for chat, _ in waha.sent][-1] == "22370000003@c.us"
    assert len(waha.sent) == 3  # the two already written to are not written to again


def test_the_team_commands_each_send():
    """One press, one message. Nothing runs on its own: this is the only thing Jeli sends to
    people who did not ask, and whoever presses should read the first before sending the second."""
    store, waha = Store(), Waha(MEMBERS)
    said = asyncio.run(send_campaign(waha, store, None, "g@g.us", limit=1))
    assert len(waha.sent) == 1 and "written to 1, 2 still to reach" in said
    asyncio.run(send_campaign(waha, store, None, "g@g.us", limit=1))
    assert [chat for chat, _ in waha.sent] == ["22370000001@c.us", "22370000002@c.us"]
    # And nothing in the module starts sending by itself.
    import app.jobs.campaign as module

    assert not hasattr(module, "start")


def test_when_nothing_could_be_sent_the_team_is_told_why():
    store, waha = Store(), Waha(MEMBERS)
    waha.paused = True
    said = asyncio.run(send_campaign(waha, store, None, "g@g.us", limit=1))
    assert "nothing was sent" in said and waha.sent == []


def test_a_paused_jeli_writes_to_nobody():
    store, waha = Store(), Waha(MEMBERS)
    waha.suspended = True
    asyncio.run(send_campaign(waha, store, None, "g@g.us"))
    assert waha.sent == [] and not store.kept.get(CAMPAIGN)


def test_the_team_sees_the_shape_of_it_before_pressing():
    import inspect

    from app.web import pages

    card = inspect.getsource(pages._campaign_card)
    assert "still to write to" in card and "already written to" in card
    assert "Left out of this group" in card  # who will not receive it, and why
    assert 'name="how_many"' in card  # the team says how many go now
    assert "Nothing is sent on its own" in card
    assert "confirm=" in card  # pressing it asks once more
    # The group is chosen, never guessed: Jeli is in several and the cohort is not always first.
    assert 'name="group"' in card and "people</option>" in card
    # Only the chosen group is counted: asking WhatsApp for every id of every group on each page
    # load would be a thousand questions nobody asked for.
    assert "Only the chosen group is counted" in card


def test_a_number_whatsapp_has_not_volunteered_is_asked_for():
    """Measured 26 September: the card offered nobody at all — six people in a group, six numbers
    unknown. A group's people arrive as account ids, and the pairs WhatsApp volunteers at startup
    do not cover them. WAHA answers for one id at a time, so Jeli asks."""
    waha = Waha([{"id": "77770001@lid"}, {"id": "77770002@lid"}],
                behind={"77770001": "22370000009"})
    numbers, why = asyncio.run(who_to_write_to(waha, "g@g.us", already=set()))
    assert numbers == ["22370000009"]          # the one WhatsApp could name
    assert why["number unknown"] == 1          # and the one it could not, left alone
    assert set(waha.asked) == {"77770001", "77770002"}


def test_asking_is_bounded_so_a_big_group_is_neither_slow_nor_a_burst():
    from app.jobs.campaign import AT_A_TIME

    assert 1 < AT_A_TIME <= 16
    import inspect

    from app.jobs import campaign

    asking = inspect.getsource(campaign.who_to_write_to)
    assert "asyncio.Semaphore(AT_A_TIME)" in asking and "asyncio.gather" in asking


def test_a_member_named_by_number_needs_no_question_at_all():
    """WhatsApp often gives the number outright; that path must not cost a round trip."""
    waha = Waha([{"id": "1111@lid", "pn": "22370000002@c.us"}, {"id": "22370000001@c.us"}])
    numbers, _ = asyncio.run(who_to_write_to(waha, "g@g.us", already=set()))
    assert numbers == ["22370000002", "22370000001"]
    assert "1111" not in waha.asked  # its number was written on the tin


def test_numbers_typed_in_by_hand_get_the_message_and_the_voice_note():
    """Measured 26 September: WhatsApp would not name the members of the cohort's group, so the
    team reads the numbers off their own phone instead. Same message, same voice note."""
    from app.jobs.campaign import send_to_numbers

    waha, store = Waha([]), Store()
    said = asyncio.run(send_to_numbers(waha, store, Voice(), "+223 60 55 77 61\n22370000002"))
    assert [chat for chat, _ in waha.sent] == ["22360557761@c.us", "22370000002@c.us"]
    assert [chat for chat, _ in waha.voices] == ["22360557761@c.us", "22370000002@c.us"]
    assert "written to 2" in said


def test_a_number_written_to_by_hand_is_never_written_to_again():
    """The two ways of sending share one list, in both directions: the group button will not write
    to someone the team typed in, and a second press of the typed form sends nothing."""
    from app.jobs.campaign import send_to_numbers

    waha, store = Waha([{"id": "22370000002@c.us"}]), Store()
    asyncio.run(send_to_numbers(waha, store, None, "22370000002"))
    assert store.kept[CAMPAIGN] == ["22370000002"]

    again = asyncio.run(send_to_numbers(waha, store, None, "22370000002"))
    assert len(waha.sent) == 1 and "nothing to send" in again
    # and the group button leaves them out too
    numbers, why = asyncio.run(who_to_write_to(waha, "g@g.us", already=set(store.kept[CAMPAIGN])))
    assert numbers == [] and why["already written to"] == 1


def test_something_that_is_not_a_phone_number_is_said_so_not_sent():
    """A pasted name or a half-copied number must not become a message to a stranger."""
    from app.jobs.campaign import send_to_numbers

    waha, store = Waha([]), Store()
    said = asyncio.run(send_to_numbers(waha, store, None, "Brendah\n12"))
    assert waha.sent == [] and "did not look like a phone number" in said


def test_a_number_copied_off_a_phone_with_its_spaces_is_one_number():
    """How a number is actually pasted: +223 60 55 77 61. Splitting on spaces made five people."""
    from app.jobs.campaign import send_to_numbers

    waha, store = Waha([]), Store()
    asyncio.run(send_to_numbers(waha, store, None, "+223 60 55 77 61"))
    assert [chat for chat, _ in waha.sent] == ["22360557761@c.us"]
