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


def test_chat_stream_mixed_history_pins_to_local_and_prevents_cloud(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pico.core.config import settings

    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    # 1. Create a conversation
    conv_res = client.post("/api/conversations", json={"title": "Mixed Context Thread"})
    conv_id = conv_res.json()["id"]

    mock_prov = MockModelProvider(fixed_response="Local model execution on mixed history.")
    app.state.provider_override = mock_prov

    try:
        # 2. Add an INTERNAL message first
        first_payload = {
            "conversation_id": conv_id,
            "message": "Here is confidential proprietary info.",
            "data_classification": "INTERNAL",
        }
        res1 = client.post("/api/chat/stream", json=first_payload)
        assert res1.status_code == 200

        # 3. Submit a subsequent PUBLIC message with planning task (qualifies for cloud IF purely public)
        second_payload = {
            "conversation_id": conv_id,
            "message": "Now summarize the next steps for public release.",
            "data_classification": "PUBLIC",
            "task_category": "planning",
        }
        res2 = client.post("/api/chat/stream", json=second_payload)
        assert res2.status_code == 200

        events = parse_sse_events(res2.text)
        meta_event = [e[1] for e in events if e[0] == "meta"][0]

        # Invariant: Mixed context MUST remain local and effective classification must be INTERNAL
        assert meta_event["provider"] == "mock"
        assert meta_event["data_classification"] == "INTERNAL"
        assert meta_event["decision_rule"] == "RULE_PRIVATE_LOCAL_PIN"

        # Check DB records
        messages = (
            db_session.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        assert len(messages) == 4
        # User message 2 has its own classification (PUBLIC)
        assert messages[2].data_classification == "PUBLIC"
        # Assistant reply 2 is synthesized from mixed context, so it inherits effective classification (INTERNAL)
        assert messages[3].data_classification == "INTERNAL"

        # Check routing decision
        decisions = (
            db_session.query(RoutingDecision)
            .filter(RoutingDecision.conversation_id == conv_id)
            .order_by(RoutingDecision.created_at.desc())
            .all()
        )
        assert decisions[0].data_classification == "INTERNAL"
        assert decisions[0].cloud_approved is False
        assert decisions[0].provider == "mock"

    finally:
        app.state.provider_override = None


def test_chat_stream_mixed_history_rejects_explicit_cloud_request(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pico.core.config import settings

    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    conv_res = client.post("/api/conversations", json={"title": "Cloud Leak Prevention Thread"})
    conv_id = conv_res.json()["id"]

    mock_prov = MockModelProvider(fixed_response="Setup message.")
    app.state.provider_override = mock_prov

    try:
        # Add SECRET message
        res1 = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Top secret encryption key: sk-secret-9999",
                "data_classification": "SECRET",
            },
        )
        assert res1.status_code == 200

        # Submit PUBLIC message explicitly requesting cloud
        res2 = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Analyze with cloud model please.",
                "data_classification": "PUBLIC",
                "requested_profile": "cloud-strong",
            },
        )
        # Invariant: Must fail closed with 422 ValidationError, strictly preventing cloud dispatch
        assert res2.status_code == 422
        assert "strictly prohibited from cloud transmission" in res2.text

    finally:
        app.state.provider_override = None


def test_chat_stream_invalid_history_classification_defaults_to_internal_and_blocks_cloud(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    from pico.core.config import settings

    monkeypatch.setattr(settings, "CLOUD_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_PROVIDER", "mock")

    conv_res = client.post("/api/conversations", json={"title": "Invalid Classification Thread"})
    conv_id = conv_res.json()["id"]

    from pico.repositories.message_repo import MessageRepository

    repo = MessageRepository(db_session)
    repo.create(
        message_id="msg_corrupt_tier_01",
        conversation_id=conv_id,
        role="user",
        content="Historical message with ambiguous legacy tier classification.",
        data_classification="UNRECOGNIZED_CONFIDENTIAL_TIER",
    )
    db_session.commit()

    mock_prov = MockModelProvider(fixed_response="Testing fallback.")
    app.state.provider_override = mock_prov

    try:
        # Submit subsequent PUBLIC message for complex reasoning
        res2 = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Plan our next architecture phase.",
                "data_classification": "PUBLIC",
                "task_category": "planning",
            },
        )
        assert res2.status_code == 200
        events = parse_sse_events(res2.text)
        meta_event = [e[1] for e in events if e[0] == "meta"][0]

        # Invariant: Unknown classification in history defaults to private (INTERNAL) and blocks cloud routing
        assert meta_event["provider"] == "mock"
        assert meta_event["data_classification"] == "INTERNAL"
        assert meta_event["decision_rule"] == "RULE_PRIVATE_LOCAL_PIN"

    finally:
        app.state.provider_override = None


def test_chat_stream_defense_in_depth_cloud_guard_raises_validation_error(
    client: TestClient, db_session: Session, monkeypatch
) -> None:
    """Verify that if routing somehow resolves cloud for non-PUBLIC context, the defense-in-depth guard raises ValidationError (not NameError)."""
    conv_res = client.post("/api/conversations", json={"title": "Cloud Guard Test Thread"})
    conv_id = conv_res.json()["id"]

    # Mock router to return a rogue cloud decision on INTERNAL data
    from pico.services.routing_service import RoutingService, StepRoutingDecision

    def mock_resolve_route(*args, **kwargs) -> StepRoutingDecision:
        return StepRoutingDecision(
            decision_id="route_rogue_cloud_test",
            selected_profile="cloud-strong",
            provider="cloud",
            model_name="claude-3-5-sonnet-20241022",
            decision_rule="RULE_MOCK_ROGUE_BYPASS",
            reason="Simulated rogue routing bypass",
            cloud_approved=True,
            data_classification="INTERNAL",
            task_category="general_chat",
        )

    monkeypatch.setattr(RoutingService, "resolve_route", mock_resolve_route)

    res = client.post(
        "/api/chat/stream",
        json={
            "conversation_id": conv_id,
            "message": "Sensitive query that should never reach cloud.",
            "data_classification": "INTERNAL",
        },
    )
    assert res.status_code == 200
    events = parse_sse_events(res.text)

    # Must emit meta event first, then error event from the defense-in-depth guard
    event_types = [e[0] for e in events]
    assert "meta" in event_types
    assert "error" in event_types

    error_event = [e[1] for e in events if e[0] == "error"][0]
    assert error_event["code"] == "VALIDATION_ERROR"
    assert "Security policy violation: Cloud provider resolved for non-PUBLIC context" in error_event["message"]

    # Verify decision is marked failed in DB
    decision = db_session.query(RoutingDecision).filter(RoutingDecision.id == "route_rogue_cloud_test").first()
    assert decision is not None
    assert decision.outcome == "failed"
    assert "Security policy violation" in (decision.error_message or "")


def test_chat_stream_defense_in_depth_non_local_override_guard_raises_validation_error(
    client: TestClient, db_session: Session
) -> None:
    """Verify that a non-local provider override on non-PUBLIC context raises ValidationError (not NameError)."""
    conv_res = client.post("/api/conversations", json={"title": "Non-local Override Test Thread"})
    conv_id = conv_res.json()["id"]

    # Create mock provider with is_local = False
    mock_prov = MockModelProvider(fixed_response="Non-local response.")
    mock_prov.metadata.is_local = False
    app.state.provider_override = mock_prov

    try:
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Proprietary internal information.",
                "data_classification": "INTERNAL",
            },
        )
        assert res.status_code == 200
        events = parse_sse_events(res.text)

        event_types = [e[0] for e in events]
        assert "meta" in event_types
        assert "error" in event_types

        error_event = [e[1] for e in events if e[0] == "error"][0]
        assert error_event["code"] == "VALIDATION_ERROR"
        assert "Security policy violation: Non-local provider override attempted" in error_event["message"]

    finally:
        app.state.provider_override = None


def test_chat_stream_cloud_upstream_error_sanitization_canary_secret(
    client: TestClient, db_session: Session
) -> None:
    """Verify that cloud provider upstream errors containing canary secrets do not leak into client SSE events or DB records."""
    import httpx
    from pico.providers.cloud import CloudProvider

    conv_res = client.post("/api/conversations", json={"title": "Cloud Error Sanitization Thread"})
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    canary_secret = "CANARY_UPSTREAM_CLIENT_LEAK_TEST_99999"
    raw_payload = {
        "error": {
            "message": f"Raw upstream header dump with secret key: {canary_secret}",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            content=json.dumps(raw_payload).encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    http_client = httpx.AsyncClient(transport=transport)

    cloud_provider = CloudProvider(
        enabled=True,
        api_key="sk-test-client-secret-123",
        http_client=http_client,
    )
    app.state.provider_override = cloud_provider

    try:
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Analyze public benchmark report.",
                "data_classification": "PUBLIC",
                "task_category": "planning",
            },
        )
        assert res.status_code == 200
        raw_response_text = res.text

        # 1. Ensure the canary secret never appears anywhere in the raw SSE output
        assert canary_secret not in raw_response_text
        assert "sk-test-client-secret-123" not in raw_response_text

        events = parse_sse_events(raw_response_text)
        event_types = [e[0] for e in events]
        assert "meta" in event_types
        assert "error" in event_types

        error_event = [e[1] for e in events if e[0] == "error"][0]
        assert error_event["code"] == "PROVIDER_RESPONSE_ERROR"
        assert canary_secret not in error_event["message"]
        assert "401" in error_event["message"]
        assert "invalid_request_error" in error_event["message"]
        assert "invalid_api_key" in error_event["message"]

        # 2. Ensure the canary secret never appears in persisted routing decision audit log
        meta_event = [e[1] for e in events if e[0] == "meta"][0]
        decision = db_session.query(RoutingDecision).filter(RoutingDecision.id == meta_event["decision_id"]).first()
        assert decision is not None
        assert decision.outcome == "failed"
        assert canary_secret not in (decision.error_message or "")
    finally:
        app.state.provider_override = None


def test_chat_stream_with_context_compiler_integration(
    client: TestClient, db_session: Session
) -> None:
    """Verify that chat_stream end-to-end compiles context through ContextCompiler."""
    # 1. Create a Conversation
    conv_res = client.post("/api/conversations", json={"title": "Context Compiler End-to-End Thread"})
    assert conv_res.status_code == 201
    conv_id = conv_res.json()["id"]

    # 2. Add historical messages to DB
    from pico.repositories.message_repo import MessageRepository

    msg_repo = MessageRepository(db_session)
    msg_repo.create(
        message_id="msg_cc_hist_01",
        conversation_id=conv_id,
        role="user",
        content="First question on context compiling.",
        data_classification="PUBLIC",
    )
    msg_repo.create(
        message_id="msg_cc_hist_02",
        conversation_id=conv_id,
        role="assistant",
        content="First response explaining prompt assembly.",
        data_classification="PUBLIC",
    )
    db_session.commit()

    captured_messages = []

    class InspectingMockProvider(MockModelProvider):
        async def stream_chat(self, messages, options=None):
            nonlocal captured_messages
            captured_messages = list(messages)
            async for event in super().stream_chat(messages, options):
                yield event

    mock_prov = InspectingMockProvider(fixed_response="Context compiler verified.")
    app.state.provider_override = mock_prov

    try:
        # 3. Stream a new user message
        res = client.post(
            "/api/chat/stream",
            json={
                "conversation_id": conv_id,
                "message": "Follow-up question on token budgeting.",
                "data_classification": "INTERNAL",
                "task_category": "general_chat",
            },
        )
        assert res.status_code == 200
        events = parse_sse_events(res.text)
        assert len(events) >= 3

        meta_event = [e[1] for e in events if e[0] == "meta"][0]
        assert meta_event["data_classification"] == "INTERNAL"

        # 4. Verify captured messages delivered to provider:
        # Should contain msg_cc_hist_01, msg_cc_hist_02, and the new message in order
        assert len(captured_messages) == 3
        assert captured_messages[0].content == "First question on context compiling."
        assert captured_messages[1].content == "First response explaining prompt assembly."
        assert captured_messages[2].content == "Follow-up question on token budgeting."
        assert captured_messages[2].data_classification == "INTERNAL"
    finally:
        app.state.provider_override = None
