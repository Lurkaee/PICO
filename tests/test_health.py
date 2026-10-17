from fastapi.testclient import TestClient


def test_health_check(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["version"] == "0.1.0"
    assert data["database"]["connected"] is True
    assert data["database"]["foreign_keys"] is True
    assert data["database"]["journal_mode"] == "wal"
    assert "timestamp" in data
