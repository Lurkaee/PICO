import json
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from pico.main import app
from pico.models.message import Message
from pico.models.routing_decision import RoutingDecision
from pico.providers.mock import MockModelProvider


def parse_sse_events(response_text: str) -> list[tuple[str, dict]]:
    """Parse text/event-stream response into a list of (event_type, data_dict)."""
    events = []
    current_event = None
    current_data = []

    for line in response_text.splitlines():
        if line.startswith("event:"):
            current_event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:") :].strip())
        elif line == "":
            if current_event and current_data:
                events.append((current_event, json.loads("\n".join(current_data))))
            current_event = None
            current_data = []

    if current_event and current_data:
        events.append((current_event, json.loads("\n".join(current_data))))

    return events


def test_chat_stream_success_lifecycle(client: TestClient, db_session: Session) -> None:
    # 1. Create a conversation
    conv_res = client.post("/api/conversations", json={"title": "Streaming Test Thread"})
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    # 2. Configure mock provider
    mock_prov = MockModelProvider(fixed_response="Deterministic stream verified.")
    app.state.provider_override = mock_prov

    try:
        # 3. Post to streaming endpoint
        stream_payload = {
            "conversation_id": conv_id,
            "message": "Verify streaming pipeline.",
            "data_classification": "INTERNAL",
            "task_category": "general_chat",
        }
        res = client.post("/api/chat/stream", json=stream_payload)
        assert res.status_code == 200
        assert "text/event-stream" in res.headers["content-type"]

        # 4. Parse SSE events
        events = parse_sse_events(res.text)
        assert len(events) >= 3

        event_types = [e[0] for e in events]
        assert "meta" in event_types
        assert "token" in event_types
        assert "done" in event_types

        # Meta event checks
        meta_event = [e[1] for e in events if e[0] == "meta"][0]
        assert meta_event["provider"] == "mock"
        assert meta_event["data_classification"] == "INTERNAL"

        # Token accumulation
        tokens = [e[1]["delta"] for e in events if e[0] == "token"]
        assert "".join(tokens) == "Deterministic stream verified."

        # Done event checks
        done_event = [e[1] for e in events if e[0] == "done"][0]
        assert done_event["message_id"] is not None
        assert done_event["finish_reason"] == "stop"

        # 5. Database state verification
        # Verify user message was persisted exactly once
        messages = (
            db_session.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        assert len(messages) == 2
        assert messages[0].role == "user"
        assert messages[0].content == "Verify streaming pipeline."
        assert messages[0].data_classification == "INTERNAL"

        # Verify assistant message was persisted with full content
        assert messages[1].role == "assistant"
        assert messages[1].content == "Deterministic stream verified."
        assert messages[1].id == done_event["message_id"]

        # Verify routing decision was persisted with outcome="success"
        decisions = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].outcome == "success"
        assert decisions[0].provider == "mock"
        assert decisions[0].data_classification == "INTERNAL"
        assert decisions[0].message_id == messages[1].id

    finally:
        app.state.provider_override = None


def test_chat_stream_nonexistent_conversation(client: TestClient) -> None:
    res = client.post(
        "/api/chat/stream",
        json={"conversation_id": "conv_missing_9999", "message": "Hello"},
    )
    assert res.status_code == 404
    assert "does not exist" in res.json()["detail"]


def test_chat_stream_inference_failure_no_fabricated_assistant_message(
    client: TestClient, db_session: Session
) -> None:
    conv_res = client.post("/api/conversations", json={"title": "Error Handling Thread"})
    conv_id = conv_res.json()["id"]

    # Configure mock provider to fail with connection error
    mock_prov = MockModelProvider(simulate_error="connection")
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "This request will fail inference.",
                "data_classification": "INTERNAL",
            },
        )
        assert res.status_code == 200
        events = parse_sse_events(res.text)

        event_types = [e[0] for e in events]
        assert "error" in event_types

        error_event = [e[1] for e in events if e[0] == "error"][0]
        assert "Simulated mock connection failure" in error_event["message"]

        # Verify: User message was saved, BUT NO assistant message was fabricated!
        messages = (
            db_session.query(Message)
            .filter(Message.conversation_id == conv_id)
            .all()
        )
        assert len(messages) == 1
        assert messages[0].role == "user"

        # Verify: Routing decision recorded failure
        decisions = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].outcome == "failed"
        assert "connection failure" in (decisions[0].error_message or "").lower()

    finally:
        app.state.provider_override = None


def test_chat_stream_missing_classification_defaults_to_internal(
    client: TestClient, db_session: Session
) -> None:
    conv_res = client.post("/api/conversations", json={"title": "Classification Thread"})
    conv_id = conv_res.json()["id"]

    mock_prov = MockModelProvider(fixed_response="Default privacy check.")
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Privacy test without classification",
            },
        )
        assert res.status_code == 200
        events = parse_sse_events(res.text)
        meta_event = [e[1] for e in events if e[0] == "meta"][0]
        assert meta_event["data_classification"] == "INTERNAL"

        # Check DB records
        user_msg = (
            db_session.query(Message)
            .filter(Message.conversation_id == conv_id, Message.role == "user")
            .first()
        )
        assert user_msg is not None
        assert user_msg.data_classification == "INTERNAL"

    finally:
        app.state.provider_override = None


def test_chat_stream_cancellation_marks_decision_cancelled(
    client: TestClient, db_session: Session
) -> None:
    from pico.providers.errors import ProviderCancelledError
    from pico.providers.types import TokenEvent

    class CancellingMockProvider(MockModelProvider):
        async def stream_chat(self, messages, options=None):
            yield TokenEvent(delta="First part...")
            raise ProviderCancelledError("Client disconnected mid-stream")

    conv_res = client.post("/api/conversations", json={"title": "Cancellation Thread"})
    conv_id = conv_res.json()["id"]

    cancelling_prov = CancellingMockProvider()
    app.state.provider_override = cancelling_prov

    try:
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Message that will be cancelled",
            },
        )
        assert res.status_code == 200
        # No completed done event was yielded
        assert "event: done" not in res.text

        # Verify: User message exists
        messages = (
            db_session.query(Message)
            .filter(Message.conversation_id == conv_id)
            .all()
        )
        assert len(messages) == 1
        assert messages[0].role == "user"

        # Verify: Routing decision recorded outcome='cancelled'
        decisions = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .all()
        )
        assert len(decisions) == 1
        assert decisions[0].outcome == "cancelled"

    finally:
        app.state.provider_override = None
