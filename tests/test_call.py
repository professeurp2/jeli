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
    from app.web.call import MAX_CALL_SECONDS, call_page

    import inspect

    page = inspect.getsource(call_page) + inspect.getsource(__import__("app.web.call", fromlist=["_script"])._script)
    assert "searching" in page and "cherche dans la mémoire" in page
    assert "Un instant — je reprends" in inspect.getsource(__import__("app.web.call", fromlist=["x"]))
    assert MAX_CALL_SECONDS >= 600


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
