from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.models.conversation import Conversation
from pico.models.message import Message


def test_append_and_list_messages(client: TestClient) -> None:
    # Setup conversation
    conv_res = client.post("/api/conversations", json={"title": "Chat Thread"})
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    # Append first message (user)
    msg1_payload = {
        "role": "user",
        "content": "Verify system persistence.",
        "data_classification": "INTERNAL",
        "metadata": {"step": 1},
    }
    msg1_res = client.post(f"/api/conversations/{conv_id}/messages", json=msg1_payload)
    assert msg1_res.status_code == 201
    msg1_data = msg1_res.json()
    assert msg1_data["id"].startswith("msg_")
    assert msg1_data["conversation_id"] == conv_id
    assert msg1_data["role"] == "user"
    assert msg1_data["content"] == "Verify system persistence."
    assert msg1_data["data_classification"] == "INTERNAL"

    # Invariant: Persisting a user message must NOT fabricate an assistant response
    list_res = client.get(f"/api/conversations/{conv_id}/messages")
    assert list_res.status_code == 200
    list_data = list_res.json()
    assert list_data["total"] == 1
    assert len(list_data["items"]) == 1
    assert list_data["items"][0]["role"] == "user"

    # Append second message explicitly (assistant)
    msg2_payload = {
        "role": "assistant",
        "content": "Storage confirmed with SQLite WAL.",
        "data_classification": "INTERNAL",
        "token_count": 10,
    }
    msg2_res = client.post(f"/api/conversations/{conv_id}/messages", json=msg2_payload)
    assert msg2_res.status_code == 201

    # Verify both messages in chronological order
    list2_res = client.get(f"/api/conversations/{conv_id}/messages")
    assert list2_res.status_code == 200
    list2_data = list2_res.json()
    assert list2_data["total"] == 2
    assert list2_data["items"][0]["role"] == "user"
    assert list2_data["items"][1]["role"] == "assistant"


def test_message_to_nonexistent_conversation(client: TestClient) -> None:
    res = client.post(
        "/api/conversations/conv_nonexistent888/messages",
        json={"role": "user", "content": "Hello"},
    )
    assert res.status_code == 404
    assert "does not exist" in res.json()["detail"]


def test_list_messages_nonexistent_conversation(client: TestClient) -> None:
    res = client.get("/api/conversations/conv_nonexistent888/messages")
    assert res.status_code == 404
    assert "does not exist" in res.json()["detail"]


def test_message_validation_errors(client: TestClient) -> None:
    conv_res = client.post("/api/conversations", json={"title": "Test Validation"})
    conv_id = conv_res.json()["id"]

    # Invalid role
    res_bad_role = client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"role": "superuser_bot", "content": "Test"},
    )
    assert res_bad_role.status_code == 422

    # Empty content
    res_empty_content = client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"role": "user", "content": ""},
    )
    assert res_empty_content.status_code == 422


def test_cascade_delete_messages(client: TestClient, db_session: Session) -> None:
    conv_res = client.post("/api/conversations", json={"title": "To Delete"})
    conv_id = conv_res.json()["id"]

    client.post(
        f"/api/conversations/{conv_id}/messages",
        json={"role": "user", "content": "Ephemeral message"},
    )

    # Verify message exists in DB
    msg_count_before = (
        db_session.query(Message).filter(Message.conversation_id == conv_id).count()
    )
    assert msg_count_before == 1

    # Delete conversation directly in DB
    conv_obj = db_session.query(Conversation).filter(Conversation.id == conv_id).first()
    assert conv_obj is not None
    db_session.delete(conv_obj)
    db_session.commit()

    # Verify cascade deletion removed the child message
    msg_count_after = (
        db_session.query(Message).filter(Message.conversation_id == conv_id).count()
    )
    assert msg_count_after == 0
