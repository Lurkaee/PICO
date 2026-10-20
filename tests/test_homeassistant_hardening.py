import asyncio
import base64
import hashlib
import json
import math
from typing import Any
import pytest
from fastapi.testclient import TestClient
import httpx

from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.homeassistant.client import (
    HomeAssistantClient,
    MAX_SINGLE_ENTITY_RESPONSE_BYTES,
    MAX_STATES_RESPONSE_BYTES,
)
from pico.homeassistant.contracts import HomeAssistantConnectionError, HomeAssistantStatus
from pico.homeassistant.sanitizer import sanitize_entity_attributes
from pico.homeassistant.service import HomeAssistantService
from pico.homeassistant.ws_client import (
    AsyncioWebSocketTransport,
    HomeAssistantWebSocketClient,
    MAX_WS_FRAME_SIZE,
    MockWebSocketTransport,
    WS_GUID,
    is_loopback_host,
)
from pico.main import app

ConnectionError = HomeAssistantConnectionError


# =====================================================================
# 1. Transport Security Policy & Loopback Detection
# =====================================================================
def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("testclient") is True
    assert is_loopback_host("127.0.0.2") is True

    assert is_loopback_host("192.168.1.100") is False
    assert is_loopback_host("10.0.0.5") is False
    assert is_loopback_host("homeassistant.local") is False
    assert is_loopback_host("evil.com") is False
    assert is_loopback_host(None) is False


@pytest.mark.anyio
async def test_websocket_transport_rejects_insecure_remote_without_opt_in() -> None:
    transport = AsyncioWebSocketTransport(allow_insecure_transport=False)
    with pytest.raises(ValidationError) as exc:
        await transport.connect("ws://192.168.1.50:8123/api/websocket")
    assert "insecure plaintext websocket transport" in str(exc.value).lower()


# =====================================================================
# 2. WebSocket Protocol Robustness (Handshake, Frame Limits, Opcodes)
# =====================================================================
class FakeStreamReader:
    def __init__(self, data: bytes) -> None:
        self._data = bytearray(data)

    async def readuntil(self, separator: bytes = b"\r\n\r\n") -> bytes:
        idx = self._data.find(separator)
        if idx == -1:
            raise asyncio.IncompleteReadError(bytes(self._data), None)
        out = bytes(self._data[: idx + len(separator)])
        del self._data[: idx + len(separator)]
        return out

    async def readexactly(self, n: int) -> bytes:
        if len(self._data) < n:
            raise asyncio.IncompleteReadError(bytes(self._data), n)
        out = bytes(self._data[:n])
        del self._data[:n]
        return out


class FakeStreamWriter:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self._closing = True

    def is_closing(self) -> bool:
        return self._closing

    async def wait_closed(self) -> None:
        pass


@pytest.mark.anyio
async def test_ws_handshake_rejects_non_101_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_reader = FakeStreamReader(b"HTTP/1.1 400 Bad Request\r\n\r\n")
    fake_writer = FakeStreamWriter()

    async def mock_open_conn(host: str, port: int, ssl: Any = None) -> tuple[Any, Any]:
        return fake_reader, fake_writer

    monkeypatch.setattr(asyncio, "open_connection", mock_open_conn)

    transport = AsyncioWebSocketTransport()
    with pytest.raises(ConnectionError) as exc:
        await transport.connect("ws://127.0.0.1:8123/api/websocket")
    assert "rejected by server" in str(exc.value).lower()
    assert transport.is_connected() is False


@pytest.mark.anyio
async def test_ws_handshake_rejects_invalid_accept_header(monkeypatch: pytest.MonkeyPatch) -> None:
    bad_header = (
        b"HTTP/1.1 101 Switching Protocols\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Accept: invalid_signature_hash\r\n"
        b"\r\n"
    )
    fake_reader = FakeStreamReader(bad_header)
    fake_writer = FakeStreamWriter()

    async def mock_open_conn(host: str, port: int, ssl: Any = None) -> tuple[Any, Any]:
        return fake_reader, fake_writer

    monkeypatch.setattr(asyncio, "open_connection", mock_open_conn)

    transport = AsyncioWebSocketTransport()
    with pytest.raises(ConnectionError) as exc:
        await transport.connect("ws://127.0.0.1:8123/api/websocket")
    assert "sec-websocket-accept" in str(exc.value).lower()
    assert transport.is_connected() is False


@pytest.mark.anyio
async def test_ws_receive_rejects_oversized_frame() -> None:
    # Construct a frame header declaring payload length of 3 MB (> MAX_WS_FRAME_SIZE of 2 MB)
    # Opcode 0x1, unmasked, length = 127 with 3 MB value
    oversized_len = 3 * 1024 * 1024
    header = bytearray([0x81, 127])
    header.extend(oversized_len.to_bytes(8, "big"))

    transport = AsyncioWebSocketTransport()
    transport._reader = FakeStreamReader(bytes(header))  # type: ignore[assignment]
    transport._writer = FakeStreamWriter()  # type: ignore[assignment]
    transport._connected = True

    with pytest.raises(ConnectionError) as exc:
        await transport.receive_json()
    assert "exceeds maximum allowed limit" in str(exc.value).lower()
    assert transport.is_connected() is False


@pytest.mark.anyio
async def test_ws_receive_rejects_oversized_control_frame() -> None:
    # Opcode 0x9 (Ping) with payload len 126 (RFC 6455 requires <= 125 for control frames)
    header = bytes([0x89, 126, 0x00, 0x80])  # 128 bytes length
    transport = AsyncioWebSocketTransport()
    transport._reader = FakeStreamReader(header)  # type: ignore[assignment]
    transport._writer = FakeStreamWriter()  # type: ignore[assignment]
    transport._connected = True

    with pytest.raises(ConnectionError) as exc:
        await transport.receive_json()
    assert "control frame" in str(exc.value).lower()


@pytest.mark.anyio
async def test_ws_receive_rejects_unsupported_opcode() -> None:
    # Opcode 0x2 (Binary frame) which is unsupported for HA JSON client
    header = bytes([0x82, 0x05]) + b"hello"
    transport = AsyncioWebSocketTransport()
    transport._reader = FakeStreamReader(header)  # type: ignore[assignment]
    transport._writer = FakeStreamWriter()  # type: ignore[assignment]
    transport._connected = True

    with pytest.raises(ConnectionError) as exc:
        await transport.receive_json()
    assert "unsupported websocket opcode" in str(exc.value).lower()


@pytest.mark.anyio
async def test_ws_receive_rejects_malformed_json() -> None:
    # Text frame with non-JSON payload
    data = b"{this is not valid json}"
    header = bytes([0x81, len(data)]) + data
    transport = AsyncioWebSocketTransport()
    transport._reader = FakeStreamReader(header)  # type: ignore[assignment]
    transport._writer = FakeStreamWriter()  # type: ignore[assignment]
    transport._connected = True

    with pytest.raises(ConnectionError) as exc:
        await transport.receive_json()
    assert "malformed websocket json" in str(exc.value).lower()


# =====================================================================
# 3. Attribute Sanitization Hardening
# =====================================================================
def test_sanitizer_drops_unknown_numeric_and_bool_attributes() -> None:
    raw = {
        "friendly_name": "Living Room Lamp",
        "brightness": 255,
        "temperature": 21.5,
        # Unknown keys of various types must all be dropped
        "unknown_metric_int": 42,
        "unknown_flag_bool": True,
        "arbitrary_float": 3.14159,
        "custom_code": "ABC-123",
        "nested_blob": {"secret": "data"},
    }
    clean = sanitize_entity_attributes(raw)
    assert "friendly_name" in clean
    assert "brightness" in clean
    assert "temperature" in clean

    assert "unknown_metric_int" not in clean
    assert "unknown_flag_bool" not in clean
    assert "arbitrary_float" not in clean
    assert "custom_code" not in clean
    assert "nested_blob" not in clean


def test_sanitizer_rejects_non_finite_floats() -> None:
    raw = {
        "temperature": float("nan"),
        "humidity": float("inf"),
        "battery": float("-inf"),
        "friendly_name": "Weather Sensor",
    }
    clean = sanitize_entity_attributes(raw)
    assert "temperature" not in clean
    assert "humidity" not in clean
    assert "battery" not in clean
    assert clean["friendly_name"] == "Weather Sensor"


def test_sanitizer_filters_sensitive_credentials_and_network_ids() -> None:
    raw = {
        "friendly_name": "Gateway",
        "token": "token_123",
        "access_token": "token_456",
        "api_password": "password",
        "secret_key": "topsecret",
        "mac_address": "AA:BB:CC:DD:EE:FF",
        "ip_address": "192.168.1.1",
        "user_id": "usr_99",
        "email": "user@example.com",
        "latitude": 37.77,
        "longitude": -122.41,
    }
    clean = sanitize_entity_attributes(raw)
    assert len(clean) == 1
    assert clean["friendly_name"] == "Gateway"


def test_sanitizer_enforces_bounds_and_limits() -> None:
    oversized_list = [1, 2, 3, 4, 5, 6, 7]  # > 5 items
    huge_string = "x" * 200

    raw = {
        "friendly_name": huge_string,
        "hvac_modes": oversized_list,
    }
    clean = sanitize_entity_attributes(raw)
    assert len(clean["friendly_name"]) == 128
    assert "hvac_modes" not in clean  # List with > 5 items dropped


# =====================================================================
# 4. Service Overview Effective Classification Invariant
# =====================================================================
@pytest.mark.anyio
async def test_service_overview_sensitive_entity_beyond_highlights_elevates_classification() -> None:
    """If entity #25 is SENSITIVE (e.g. lock.front_door), overview must be SENSITIVE even if highlights only show lights."""
    # Generate 20 light entities followed by 1 lock entity
    mock_states = []
    for i in range(20):
        mock_states.append({
            "entity_id": f"light.bulb_{i:02d}",
            "state": "on",
            "attributes": {"friendly_name": f"Bulb {i}"},
        })
    # Entity #21 is a lock (SENSITIVE)
    mock_states.append({
        "entity_id": "lock.front_door",
        "state": "locked",
        "attributes": {"friendly_name": "Front Door Deadbolt"},
    })

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_states)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        ha_client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="test_token",
            http_client=http_client,
        )
        service = HomeAssistantService(client=ha_client)
        overview = await service.get_overview()

        # Highlights capped at 20
        assert len(overview.highlights) == 20
        # All highlights happen to be lights (INTERNAL)
        assert all(h.domain == "light" for h in overview.highlights)
        # Total entities includes the lock
        assert overview.total_entities == 21
        assert overview.domain_counts["lock"] == 1
        # The overview classification MUST be SENSITIVE because the lock contributed to the summary!
        assert overview.data_classification == "SENSITIVE"


# =====================================================================
# 5. REST Response Size Bounds & Malformed Payload Handling
# =====================================================================
@pytest.mark.anyio
async def test_rest_states_rejects_oversized_payload() -> None:
    """Inbound states response exceeding byte limit is rejected before unbounded parsing."""
    chunk = b'{"entity_id": "sensor.test", "state": "1"},'

    class StreamingAsyncByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b"["
            for _ in range(70000):
                yield chunk
            yield b'{"entity_id": "sensor.end", "state": "0"}]'

    def streaming_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=StreamingAsyncByteStream())

    transport = httpx.MockTransport(streaming_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        with pytest.raises(ValidationError) as exc:
            await client.get_states()
        assert "exceeded maximum allowed limit" in str(exc.value).lower()




@pytest.mark.anyio
async def test_rest_states_rejects_oversized_content_length_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(5 * 1024 * 1024)}, content=b"[]")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        with pytest.raises(ValidationError) as exc:
            await client.get_states()
        assert "content-length" in str(exc.value).lower()


@pytest.mark.anyio
async def test_rest_states_rejects_malformed_truncated_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'[{"entity_id": "light.broken", "state": "o')

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        with pytest.raises(ValidationError) as exc:
            await client.get_states()
        assert "malformed or truncated" in str(exc.value).lower()


@pytest.mark.anyio
async def test_rest_states_rejects_missing_entity_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"state": "on", "attributes": {}}])

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        with pytest.raises(ValidationError) as exc:
            await client.get_states()
        assert "missing 'entity_id'" in str(exc.value).lower()


# =====================================================================
# 6. Household API Remote Exposure Boundary Tests
# =====================================================================
def test_household_api_exposure_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    api_client = TestClient(app)

    # 1. Local loopback client is permitted
    resp_local = api_client.get("/api/homeassistant/status")
    assert resp_local.status_code == 200

    # 2. Remote client without token is strictly rejected with 403 Forbidden
    resp_remote_no_token = api_client.get(
        "/api/homeassistant/status",
        headers={"X-Forwarded-For": "192.168.1.50"},
    )
    # Simulate client host from remote
    remote_client = TestClient(app, client=("192.168.1.50", 45000))

    resp_remote = remote_client.get("/api/homeassistant/status")
    assert resp_remote.status_code == 403
    assert "restricted" in resp_remote.json()["detail"].lower()

    # Remote access to entities endpoint also blocked
    resp_entities = remote_client.get("/api/homeassistant/entities")
    assert resp_entities.status_code == 403

    # Remote access to overview endpoint also blocked
    resp_overview = remote_client.get("/api/homeassistant/overview")
    assert resp_overview.status_code == 403

    # 3. Remote client with invalid token rejected with 403
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", "valid_operator_token_999")
    resp_bad_token = remote_client.get(
        "/api/homeassistant/status",
        headers={"X-Pico-Approval-Token": "wrong_token"},
    )
    assert resp_bad_token.status_code == 403

    # 4. Remote client with valid token permitted (200 OK)
    resp_good_token = remote_client.get(
        "/api/homeassistant/status",
        headers={"X-Pico-Approval-Token": "valid_operator_token_999"},
    )
    assert resp_good_token.status_code == 200

    # 5. Remote client fails closed when APPROVAL_SECRET_TOKEN is None even if token header is sent
    monkeypatch.setattr(settings, "APPROVAL_SECRET_TOKEN", None)
    resp_no_token_configured = remote_client.get(
        "/api/homeassistant/status",
        headers={"X-Pico-Approval-Token": "any_token"},
    )
    assert resp_no_token_configured.status_code == 403
