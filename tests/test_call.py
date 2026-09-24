"""Calling Jeli from a browser: no phone number, no app, no account."""

import json

from fastapi.testclient import TestClient

from app.main import app


def test_anyone_can_open_the_call_page():
    with TestClient(app) as client:
        page = client.get("/jeli/call")
    assert page.status_code == 200
    assert "Parlez à Jeli" in page.text and 'aria-label="Appeler Jeli"' in page.text
    # The microphone is asked for only when the call starts, and released when it ends.
    assert "getUserMedia" in page.text and "track.stop()" in page.text


def test_a_call_answers_from_the_memory_not_from_general_knowledge():
    from app.web.call import SEARCH_TOOL, SPOKEN

    assert "search_memory" in SPOKEN and "never invent a date" in SPOKEN
    declared = SEARCH_TOOL.function_declarations[0]
    assert declared.name == "search_memory"
    assert "question" in declared.parameters.required


def test_a_call_that_finds_no_engine_says_so_instead_of_hanging():
    with TestClient(app) as client:
        app.state.llm = None
        with client.websocket_connect("/jeli/call/ws") as socket:
            note = socket.receive_json()
    assert note["end"] is True and "ne peut pas prendre l'appel" in note["state"]


def test_the_chat_has_a_call_button_where_whatsapp_puts_it():
    from app.web import ui

    assert "call" in ui.ICONS
    with TestClient(app) as client:
        client.post("/login", data={"name": "stanley", "password": "x"})
    # The header markup carries the link even when signed out is impossible to render.
    import inspect

    from app.web import pages

    source = inspect.getsource(pages)
    assert 'class="wa-call" href="/jeli/call"' in source


def test_a_call_survives_a_session_ending():
    """Measured 23 Sep: the call stopped mid-conversation with no warning. A Live session ends when
    its context fills; the window now slides instead, and a handle reopens what did end."""
    from app.web.call import _config

    config = _config("instructions", handle=None)
    assert config.context_window_compression is not None
    assert config.context_window_compression.sliding_window is not None
    assert config.session_resumption is not None
    # Reopening carries the handle, so the conversation is picked up, not started again.
    assert _config("instructions", handle="abc").session_resumption.handle == "abc"


def test_the_caller_is_told_what_is_happening():
    """The page said nothing while Jeli searched, and nothing when the call ended."""
    import inspect

    from app.web import call

    page = inspect.getsource(call.call_page) + call.CALL_SCRIPT
    assert "searching" in page and "cherche dans la mémoire" in page
    assert "je reprends" in call.RESUMING and "RESUMING" in inspect.getsource(call.call_socket)


def test_a_session_ending_is_a_reconnection_not_a_hang_up():
    """Measured 23 Sep: the call "marche bien mais ce n'est pas continu — il coupe sans prévenir".

    A Live session ends every few minutes; only a *new* handle arrived with some of them. The loop
    wrote `handle = await _talk(...)`, so the second ending overwrote the good handle with None —
    and a None handle hung up. Now the last handle is kept, and no ending hangs up by itself.
    """
    import inspect

    source = inspect.getsource(__import__("app.web.call", fromlist=["x"]).call_socket)
    assert "await _talk(socket, session, state, line) or handle" in source
    # Nothing in the loop may end the call merely because there is nothing to resume with.
    assert "if handle is None" not in source
    # A call now ends for a reason about the caller or the engine, never a stopwatch.
    assert "MAX_CALL_SECONDS" not in inspect.getsource(__import__("app.web.call", fromlist=["x"]))


def test_a_call_holds_one_session_at_a_time_and_never_two():
    """The old code opened a session only to test the model, closed it, and opened a second one.

    Two sessions per call, against a free tier that allows very few at once: the third caller of the
    evening was told Jeli could not take the call.
    """
    from app.web import call

    assert not hasattr(call, "_first_model_that_answers")


def test_the_caller_leaving_ends_the_call():
    """A closed tab left its Live session running to the stopwatch, holding a slot nobody could use."""
    import asyncio
    import inspect

    from app.web.call import Line, _talk

    class Hung:
        async def receive(self):
            raise RuntimeError("the tab is gone")

    line = Line(Hung())
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(line.listen())
    assert line.gone.is_set()
    # And a session in progress ends with the caller, instead of talking to an empty room.
    assert "line.gone.wait()" in inspect.getsource(_talk)


def test_a_call_ends_on_silence_not_on_a_stopwatch():
    import asyncio

    from app.web import call

    async def run():
        line = call.Line(object())
        line.spoke_at -= call.QUIET_SECONDS + 1
        await asyncio.wait_for(call._hang_up_on_silence(line), timeout=2)
        return line

    line = asyncio.run(run())
    assert line.gone.is_set() and line.quiet is True
    assert call.QUIET_SECONDS >= 120  # a pause to think is not a hang-up


def test_google_asking_to_reconnect_is_acted_on_not_waited_out():
    """Google warns seconds before it closes. Reopening then is a pause; waiting is a dropped call."""
    import inspect

    from app.web.call import _talk

    source = inspect.getsource(_talk)
    warned = source.index("go_away")
    assert "return" in source[warned : warned + 400]


def test_the_transcript_is_one_row_per_turn_not_per_fragment():
    """"Jeli / demandé", "Jeli / un récap", "Jeli / de la" was one sentence, cut to pieces."""
    import inspect

    from app.web import call

    script = call.CALL_SCRIPT
    assert "open[who]" in script and "row.textContent + ' ' + text" in script
    assert "message.turn" in script  # a finished sentence closes its row


def test_jeli_says_it_can_be_called():
    from app.answer.persona import CAPABILITIES
    from app.jobs.greetings import HELLO_TEXT

    assert "Be called and talked to out loud" in CAPABILITIES
    assert "never invent one" in CAPABILITIES  # the address comes from its state, not from guessing
    assert "m'appeler et me parler de vive voix" in HELLO_TEXT


# --- What the team controls, and what the page shows ------------------------------------------


class Chosen(dict):
    """A runtime the team has set; anything not named falls back to the field's default."""

    def __missing__(self, key):
        from app.control.runtime import FIELDS
        from app.config import get_settings

        return FIELDS[key].default(get_settings())


class Knowing:
    async def knowledge_overview(self):
        return {
            "chats": [
                {"chat_id": "1@g.us", "messages": 9312, "last_message": None, "live": 1},
                {"chat_id": "2@g.us", "messages": 2461, "last_message": None, "live": 1},
                {"chat_id": "22370000000@c.us", "messages": 74, "last_message": None, "live": 1},
            ],
            "recordings": [{"id": "a", "method": "gemini"}, {"id": "b", "method": "link"}],
        }


def test_the_page_says_what_jeli_keeps_before_anyone_has_to_believe_it():
    with TestClient(app) as client:
        app.state.store = Knowing()
        page = client.get("/jeli/call").text
    # The figures are real, read from the memory itself — and a thin space, not a comma.
    assert "11 847" in page and "messages en mémoire" in page
    assert ">2<" in page and "groupes suivis" in page  # the private chat is not a group
    assert "sessions transcrites" in page  # a link Jeli cannot watch is not a transcription


def test_the_team_chooses_what_the_page_offers_to_ask():
    with TestClient(app) as client:
        app.state.runtime = Chosen({"call_questions": ["Et les échéances, c'est quand ?", "Who runs MIT UAI?"]})
        page = client.get("/jeli/call").text
    # A comma inside a question must survive: it is one question, not two.
    assert 'data-ask="Et les échéances, c&#x27;est quand ?"' in page
    assert 'data-ask="Who runs MIT UAI?"' in page
    assert "C&#x27;est quand la prochaine session ?" not in page  # the defaults are replaced, not added to


def test_the_team_can_close_the_line():
    with TestClient(app) as client:
        app.state.runtime = Chosen({"enabled.calls": False})
        page = client.get("/jeli/call")
        with client.websocket_connect("/jeli/call/ws") as socket:
            note = socket.receive_json()
    assert page.status_code == 200 and "La ligne est fermée" in page.text
    assert "aria-label=\"Appeler Jeli\"" not in page.text  # no button that cannot work
    assert note["end"] is True and note["state"] == "Jeli ne prend pas d'appels pour le moment."


def test_a_closed_line_is_not_offered_by_jeli_either():
    """Switched off, Jeli must stop handing out a number nobody can pick up."""
    import asyncio

    from app.answer.awareness import Awareness

    class Empty:
        async def knowledge_overview(self):
            return {"chats": []}

        async def all_recordings(self):
            return []

        async def deadlines_between(self, *args, **kwargs):
            return []

        async def list_documents(self):
            return []

    closed = asyncio.run(Awareness(Empty(), None, runtime=Chosen({"enabled.calls": False})).state())
    assert "/jeli/call" not in closed and "cannot take calls" in closed


def test_the_team_chooses_the_voice_the_transcript_and_the_patience():
    from app.web.call import _config, call_settings

    class State:
        runtime = Chosen({"call_voice": "kore", "call_quiet_minutes": 12, "call_transcript": False})

    chosen = call_settings(State())
    assert chosen["voice"] == "Kore" and chosen["quiet"] == 12 and chosen["transcript"] is False
    spoken = _config("instructions", None, chosen["voice"]).speech_config
    assert spoken.voice_config.prebuilt_voice_config.voice_name == "Kore"
    with TestClient(app) as client:
        app.state.runtime = State.runtime
        page = client.get("/jeli/call").text
    assert "SHOW_TRANSCRIPT = false" in page and 'id="said"' not in page


def test_a_question_tapped_on_the_page_reaches_jeli_as_a_spoken_one_would():
    """Somebody who does not know the programme has nothing to say to it, and a silent room is
    where a demo dies. The chips are asked for real, not pasted into a box."""
    import asyncio
    import inspect

    from app.web.call import CALL_SCRIPT, Line, _talk

    assert "JSON.stringify({ ask: question })" in CALL_SCRIPT
    # Not on the line yet: the page calls first, then asks it for them.
    assert "pending = chip.dataset.ask" in CALL_SCRIPT
    assert "send_client_content" in inspect.getsource(_talk)

    class Tapping:
        def __init__(self):
            self.packets = [
                {"type": "websocket.receive", "text": json.dumps({"ask": "C'est quand la session ?"})},
                {"type": "websocket.disconnect"},
            ]

        async def receive(self):
            return self.packets.pop(0)

    line = Line(Tapping())
    asyncio.run(line.listen())
    assert line.typed.get_nowait() == "C'est quand la session ?"


def test_the_page_never_grows_wider_than_the_phone_it_is_read_on():
    """Measured: one long sentence in the transcript pushed the whole page sideways, because an
    implicit grid column sizes to its widest child."""
    from app.web.call import CALL_CSS

    assert "grid-template-columns: minmax(0, 1fr)" in CALL_CSS  # the transcript
    assert "repeat(3, minmax(0, 1fr))" in CALL_CSS  # the figures
    assert "viewport-fit=cover" in __import__("inspect").getsource(__import__("app.web.call", fromlist=["x"])._shell)
    assert "prefers-reduced-motion" in CALL_CSS  # the animation can be turned off by the reader


def test_the_orb_reacts_to_real_sound_not_to_a_decorative_loop():
    from app.web.call import CALL_SCRIPT

    # Jeli's voice on the way in, and the caller's own microphone on the way out.
    assert "want = Math.max(want, loudness(channel))" in CALL_SCRIPT
    assert "want = Math.max(want, loudness(input))" in CALL_SCRIPT
    for state in ("idle", "listening", "searching", "speaking"):
        assert state + ":" in CALL_SCRIPT


def test_jeli_has_a_face_on_the_call_and_it_is_its_own():
    """A caller watching the face is watching Jeli speak. Not a stock avatar: the lemur members
    already see on WhatsApp, taken apart so each piece can move."""
    from app.web import ui
    from app.web.call import CALL_SCRIPT, JELI_FACE

    # The same pieces as the mark itself: the ears, the eye patches, the gold irises, the smile.
    for piece in ('d="M12 21 17 8l9 11z"', 'fill="#f5b301"', 'd="M29 42.5q3 3.2 6 0z"'):
        assert piece in JELI_FACE and piece in ui.LEMUR
    # The ears are drawn after the head, or the head hides them.
    assert JELI_FACE.index('id="head"') < JELI_FACE.index('id="earL"')
    assert 'stroke="#ffffff"' in JELI_FACE  # the white ring of the profile picture
    assert 'id="crown"' in JELI_FACE  # and the crown it wears there
    # The mouth is the voice: it opens on the measured loudness, and only while Jeli is speaking.
    assert "const gap = speaking ? level * 5.2 : 0;" in CALL_SCRIPT
    assert "mouth.setAttribute('ry', gap.toFixed(2));" in CALL_SCRIPT
    # Closed, it is a smile; open, it is Jeli talking. Never both.
    assert "smile.setAttribute('opacity', gap > 0.25 ? 0 : 1);" in CALL_SCRIPT


def test_the_face_has_a_mood_for_everything_the_call_can_be_doing():
    from app.web.call import CALL_SCRIPT

    for state in ("idle", "dialing", "listening", "searching", "speaking", "ended"):
        assert state + ":" in CALL_SCRIPT.split("const MOOD")[1].split("};")[0]
    # Listening perks the ears up; searching looks away, the way anyone does when thinking.
    moods = CALL_SCRIPT.split("const MOOD")[1].split("};")[0]
    assert "listening: { ears: 13" in moods and "searching: { ears: -6" in moods
    assert "blinkUntil = at + 110" in CALL_SCRIPT  # and it blinks, because things that live do


# --- Calls happening right now ----------------------------------------------------------------


def test_the_team_sees_the_calls_in_progress_and_nothing_is_kept():
    import asyncio

    from app.web import call

    call.ON_AIR.clear()
    air = call.OnAir("abc123", "gemini-2.5-flash-native-audio-latest")
    call.ON_AIR[air.id] = air
    asyncio.run(air.note({"said": "C'est quand la prochaine session ?"}))
    air.searches.append("la prochaine session")
    [row] = call.calls_in_progress()
    assert row["id"] == "abc123" and row["searches"] == 1
    assert row["asked"] == "la prochaine session" and row["followers"] == 0
    # What was said is held only while the call lasts, and only the last words of it.
    assert len(air.words) == 1 and call.KEPT_WORDS <= 200
    call.ON_AIR.clear()
    assert call.calls_in_progress() == []


def test_following_a_call_needs_to_be_signed_in():
    """The calls are the community's own members talking. Not a page anyone can open."""
    from starlette.websockets import WebSocketDisconnect as Refused

    from app.web import call

    call.ON_AIR.clear()
    call.ON_AIR["open1"] = call.OnAir("open1", "m")
    try:
        with TestClient(app) as client:
            try:
                with client.websocket_connect("/dashboard/calls/open1/listen"):
                    assert False, "a stranger was let in"
            except Refused as refused:
                assert refused.code == 1008
            # And the page itself is never served to someone who is not on the team.
            assert client.get("/dashboard/calls").status_code != 200
    finally:
        call.ON_AIR.clear()


def test_a_follower_hears_both_voices_and_reads_what_was_already_said():
    import asyncio

    from app.web import call

    class Follower:
        def __init__(self):
            self.text, self.sound = [], []

        async def send_text(self, payload):
            self.text.append(json.loads(payload))

        async def send_bytes(self, payload):
            self.sound.append(payload)

    async def run():
        air = call.OnAir("x", "m")
        follower = Follower()
        air.followers.add(follower)
        await air.note({"jeli": "Diane l'a annoncé mardi."})
        await air.share(b"\x00" + b"ab")  # the caller, 16 kHz
        await air.share(b"\x01" + b"cd")  # Jeli, 24 kHz
        return follower

    follower = asyncio.run(run())
    assert follower.text == [{"jeli": "Diane l'a annoncé mardi."}]
    # One byte says whose voice it is, because the two are not sampled at the same rate.
    assert follower.sound == [b"\x00ab", b"\x01cd"]
    from app.web.pages import CALLS_SCRIPT

    assert "bytes[0] === 1 ? 24000 : 16000" in CALLS_SCRIPT


def test_the_caller_is_told_the_team_may_listen():
    """A page that promises privacy and is listened to anyway is a page that lies."""
    with TestClient(app) as client:
        page = client.get("/jeli/call").text
    assert "Rien n'est enregistré" in page
    assert "L'équipe de Jeli peut suivre un appel en direct" in page


def test_the_call_sounds_like_jeli_everywhere_unless_told_otherwise():
    from app.control.runtime import FIELDS
    from app.web.call import call_settings

    assert FIELDS["call_voice"].default(None) == "same"

    class Following:
        runtime = Chosen({"call_voice": "same", "voice_name": "puck"})

    class OwnCharacter:
        runtime = Chosen({"call_voice": "charon", "voice_name": "puck"})

    assert call_settings(Following())["voice"] == "Puck"  # the voice of the voice notes
    assert call_settings(OwnCharacter())["voice"] == "Charon"  # unless the team asked for another
