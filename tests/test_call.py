"""Calling Jeli from a browser: no phone number, no app, no account."""

from fastapi.testclient import TestClient

from app.main import app


def test_anyone_can_open_the_call_page():
    with TestClient(app) as client:
        page = client.get("/jeli/call")
    assert page.status_code == 200
    assert "Appeler" in page.text
    # The microphone is asked for only when the call starts, and released when it ends.
    assert "getUserMedia" in page.text and "getTracks().forEach(t => t.stop())" in page.text


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

    page = inspect.getsource(call.call_page) + inspect.getsource(call._script)
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
        async def receive_bytes(self):
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

    script = inspect.getsource(call._script)
    assert "open[who]" in script and "line.textContent + ' ' + text" in script
    assert "message.turn" in script  # a finished sentence closes its row


def test_jeli_says_it_can_be_called():
    from app.answer.persona import CAPABILITIES
    from app.jobs.greetings import HELLO_TEXT

    assert "Be called and talked to out loud" in CAPABILITIES
    assert "never invent one" in CAPABILITIES  # the address comes from its state, not from guessing
    assert "m'appeler et me parler de vive voix" in HELLO_TEXT
