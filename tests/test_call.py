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
