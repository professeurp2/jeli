from fastapi.testclient import TestClient

from app.adapters.telegram import WEBHOOK_PATH
from app.main import app


def test_health_without_telegram_token():
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "telegram": False}


def test_webhook_rejects_requests_without_the_secret():
    with TestClient(app) as client:
        response = client.post(WEBHOOK_PATH, json={"update_id": 1})
    assert response.status_code == 403
