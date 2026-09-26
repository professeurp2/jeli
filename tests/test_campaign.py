"""The vote message: the one thing Jeli sends to members one by one.

Two hundred and forty private messages is both the most useful thing the team can ask of Jeli and
the surest way to lose its number. These tests hold the rails.
"""

import asyncio

from app.jobs.campaign import CAMPAIGN, CAMPAIGN_SPOKEN, CAMPAIGN_TEXT, send_campaign, who_to_write_to


class Waha:
    def __init__(self, people, admins=(), team=(), allowed=99):
        self.people, self.admins = people, set(admins)
        self.admin_numbers = list(team)
        self.suspended = self.paused = False
        self.sent, self.voices, self.allowed = [], [], allowed
        self.spacer = self

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
    assert "already written to" in said
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
    assert "usual pace" in card  # how long it will take, before starting
    assert "Left out:" in card  # and who will not receive it
    assert "confirm=" in card  # pressing it asks once more
