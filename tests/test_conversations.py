from fastapi.testclient import TestClient


def test_create_and_get_conversation(client: TestClient) -> None:
    # Create conversation
    create_payload = {
        "title": "Architecture Alignment",
        "metadata": {"source": "test_suite"},
    }
    res = client.post("/api/conversations", json=create_payload)
    assert res.status_code == 201
    data = res.json()
    assert data["title"] == "Architecture Alignment"
    assert data["id"].startswith("conv_")
    assert data["metadata"] == {"source": "test_suite"}
    assert "created_at" in data
    assert "updated_at" in data

    conv_id = data["id"]

    # Get conversation
    get_res = client.get(f"/api/conversations/{conv_id}")
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert get_data["id"] == conv_id
    assert get_data["title"] == "Architecture Alignment"


def test_get_nonexistent_conversation(client: TestClient) -> None:
    res = client.get("/api/conversations/conv_nonexistent999")
    assert res.status_code == 404
    error = res.json()
    assert "does not exist" in error["detail"]


def test_list_conversations_pagination(client: TestClient) -> None:
    # Create multiple conversations
    for i in range(5):
        client.post("/api/conversations", json={"title": f"Conv {i}"})

    # Test limit and offset
    res = client.get("/api/conversations?limit=3&offset=0")
    assert res.status_code == 200
    data = res.json()
    assert len(data["items"]) == 3
    assert data["total"] >= 5
    assert data["limit"] == 3
    assert data["offset"] == 0

    # Next page
    res_page2 = client.get("/api/conversations?limit=3&offset=3")
    assert res_page2.status_code == 200
    data_page2 = res_page2.json()
    assert len(data_page2["items"]) >= 2


def test_create_conversation_validation_error(client: TestClient) -> None:
    # Title cannot be empty
    res = client.post("/api/conversations", json={"title": ""})
    assert res.status_code == 422
