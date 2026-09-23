"""The page an IT administrator reads before deciding — open to anyone with the link."""

from fastapi.testclient import TestClient

from app.main import app


def test_the_administrator_can_read_it_without_an_account():
    """An administrator has no Jeli account, and should not need one to read a request made to them."""
    with TestClient(app) as client:
        page = client.get("/teams")
    assert page.status_code == 200
    text = page.text
    # The exact permission and the exact command, so it can be checked against Microsoft's own docs.
    assert "OnlineMeetingTranscript.Read.All" in text
    assert "New-CsApplicationAccessPolicy" in text and "Grant-CsApplicationAccessPolicy" in text
    # What it would never do, said as plainly as what it would.
    assert "never joins a meeting" in text
    assert "No mailboxes, no files, no chats" in text
    # And that saying no is a real option, with what happens then.
    assert "If the answer is no" in text


def test_it_asks_for_nothing_the_page_does_not_explain():
    """A permission granted from a chat message is a permission granted blind."""
    with TestClient(app) as client:
        text = client.get("/teams").text
    for promised in ("Where it is stored", "Who can read it back", "Removing it"):
        assert promised in text, promised


def test_the_dashboard_and_the_admin_page_are_different_doors():
    """The dashboard changes how Jeli behaves; this page changes nothing at all."""
    from app.web.teams_setup import router

    assert [r.path for r in router.routes] == ["/teams"]
    assert all("POST" not in getattr(r, "methods", set()) for r in router.routes)
