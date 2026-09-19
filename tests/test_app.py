from fastapi.testclient import TestClient

from app.adapters.telegram import WEBHOOK_PATH as TELEGRAM_WEBHOOK_PATH
from app.main import app


def test_health_without_any_channel_configured():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "whatsapp": False,
        "telegram": False,
        "database": False,
        "indexing": False,
        "answers": False,
        "daily_digest": False,
        "team_report": False,
    }


def test_telegram_webhook_rejects_requests_without_the_secret():
    with TestClient(app) as client:
        response = client.post(TELEGRAM_WEBHOOK_PATH, json={"update_id": 1})
    assert response.status_code == 403
