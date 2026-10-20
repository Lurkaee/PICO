import asyncio
import base64
import json
import os
import re
import ssl
from abc import ABC, abstractmethod
from typing import Any, Callable, Coroutine
from urllib.parse import urlparse

from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.contracts import HomeAssistantConnectionError

ConnectionError = HomeAssistantConnectionError

# Strictly approved read-only WebSocket command types
ALLOWED_READONLY_WS_COMMANDS = {
    "get_states",
    "get_config",
    "ping",
    "subscribe_events",
    "unsubscribe_events",
}


class BaseWebSocketTransport(ABC):
    @abstractmethod
    async def connect(self, ws_url: str, timeout: float = 10.0) -> None:
        pass

    @abstractmethod
    async def send_json(self, payload: dict[str, Any]) -> None:
        pass

    @abstractmethod
    async def receive_json(self, timeout: float = 10.0) -> dict[str, Any]:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        pass


class AsyncioWebSocketTransport(BaseWebSocketTransport):
    """Pure-Python asyncio RFC 6455 WebSocket client transport with zero external dependencies."""

    def __init__(self) -> None:
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected and self._writer is not None and not self._writer.is_closing()

    async def connect(self, ws_url: str, timeout: float = 10.0) -> None:
        parsed = urlparse(ws_url)
        scheme = parsed.scheme.lower()
        if scheme not in ("ws", "wss"):
            raise ValidationError(f"Invalid WebSocket URL scheme '{scheme}'. Expected 'ws' or 'wss'.")

        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if scheme == "wss" else 80)
        path = parsed.path or "/api/websocket"
        if parsed.query:
            path += f"?{parsed.query}"

        ssl_ctx: ssl.SSLContext | None = None
        if scheme == "wss":
            ssl_ctx = ssl.create_default_context()

        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_ctx),
                timeout=timeout,
            )

            # Perform HTTP Upgrade Handshake
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            self._writer.write(request.encode("utf-8"))
            await self._writer.drain()

            # Read Handshake Response
            response_bytes = await asyncio.wait_for(self._reader.readuntil(b"\r\n\r\n"), timeout=timeout)
            response_header = response_bytes.decode("utf-8", errors="replace")
            status_line = response_header.split("\r\n")[0]
            if "101" not in status_line:
                raise ConnectionError(f"WebSocket upgrade rejected by server: {status_line}")

            self._connected = True
        except (asyncio.TimeoutError, TimeoutError):
            await self.close()
            raise ConnectionError(f"WebSocket connection to '{host}:{port}' timed out after {timeout}s.")
        except Exception as exc:
            await self.close()
            raise ConnectionError(f"WebSocket connection failed: {str(exc)}")

    async def send_json(self, payload: dict[str, Any]) -> None:
        if not self.is_connected() or not self._writer:
            raise ConnectionError("WebSocket is not connected.")

        data = json.dumps(payload).encode("utf-8")
        length = len(data)

        # RFC 6455 client-to-server text frame (opcode 0x1, masked)
        header = bytearray([0x81])
        if length <= 125:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(length.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(length.to_bytes(8, "big"))

        mask = os.urandom(4)
        header.extend(mask)
        masked_payload = bytes(b ^ mask[i % 4] for i, b in enumerate(data))

        self._writer.write(bytes(header) + masked_payload)
        await self._writer.drain()

    async def receive_json(self, timeout: float = 10.0) -> dict[str, Any]:
        if not self.is_connected() or not self._reader:
            raise ConnectionError("WebSocket is not connected.")

        try:
            head = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout)
            b1, b2 = head[0], head[1]
            opcode = b1 & 0x0F
            is_masked = bool(b2 & 0x80)
            payload_len = b2 & 0x7F

            if payload_len == 126:
                ext = await asyncio.wait_for(self._reader.readexactly(2), timeout=timeout)
                payload_len = int.from_bytes(ext, "big")
            elif payload_len == 127:
                ext = await asyncio.wait_for(self._reader.readexactly(8), timeout=timeout)
                payload_len = int.from_bytes(ext, "big")

            mask = b""
            if is_masked:
                mask = await asyncio.wait_for(self._reader.readexactly(4), timeout=timeout)

            payload = await asyncio.wait_for(self._reader.readexactly(payload_len), timeout=timeout)
            if is_masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:  # Close frame
                self._connected = False
                raise ConnectionError("WebSocket closed by server.")
            elif opcode == 0x9:  # Ping frame
                # Send Pong frame
                pong = bytearray([0x8A, 0x80])
                pong_mask = os.urandom(4)
                pong.extend(pong_mask)
                self._writer.write(bytes(pong))
                await self._writer.drain()
                return await self.receive_json(timeout=timeout)

            return json.loads(payload.decode("utf-8"))
        except (asyncio.TimeoutError, TimeoutError):
            raise ConnectionError(f"WebSocket read timed out after {timeout}s.")
        except Exception as exc:
            self._connected = False
            raise ConnectionError(f"WebSocket receive error: {str(exc)}")

    async def close(self) -> None:
        self._connected = False
        if self._writer:
            try:
                # Send close frame
                self._writer.write(bytes([0x88, 0x80]) + os.urandom(4))
                await self._writer.drain()
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            finally:
                self._writer = None
                self._reader = None


class MockWebSocketTransport(BaseWebSocketTransport):
    """In-memory mock transport for deterministic testing of authentication and command correlation."""

    def __init__(
        self,
        valid_token: str = "valid_token",
        ha_version: str = "2026.3.0",
        custom_responses: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.valid_token = valid_token
        self.ha_version = ha_version
        self.custom_responses = custom_responses or {}
        self._connected = False
        self._authenticated = False
        self._incoming_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._last_sent: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, ws_url: str, timeout: float = 10.0) -> None:
        self._connected = True
        # Initial auth_required message from Home Assistant
        await self._incoming_queue.put({"type": "auth_required", "ha_version": self.ha_version})

    async def send_json(self, payload: dict[str, Any]) -> None:
        if not self._connected:
            raise ConnectionError("Mock WebSocket not connected.")
        self._last_sent.append(payload)

        msg_type = payload.get("type")
        if msg_type == "auth":
            token = payload.get("access_token")
            if token == self.valid_token:
                self._authenticated = True
                await self._incoming_queue.put({"type": "auth_ok", "ha_version": self.ha_version})
            else:
                self._authenticated = False
                await self._incoming_queue.put(
                    {"type": "auth_invalid", "message": "Invalid access token"}
                )
        elif not self._authenticated:
            await self._incoming_queue.put({"type": "auth_required"})
        elif msg_type in self.custom_responses:
            cmd_id = payload.get("id", 1)
            resp = dict(self.custom_responses[msg_type])
            resp["id"] = cmd_id
            await self._incoming_queue.put(resp)
        elif msg_type == "get_states":
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put(
                {
                    "id": cmd_id,
                    "type": "result",
                    "success": True,
                    "result": [
                        {
                            "entity_id": "light.living_room",
                            "state": "on",
                            "attributes": {"friendly_name": "Living Room Light"},
                        },
                        {
                            "entity_id": "sensor.office_temperature",
                            "state": "21.5",
                            "attributes": {
                                "friendly_name": "Office Temperature",
                                "unit_of_measurement": "°C",
                                "device_class": "temperature",
                            },
                        },
                    ],
                }
            )
        elif msg_type == "get_config":
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put(
                {
                    "id": cmd_id,
                    "type": "result",
                    "success": True,
                    "result": {"version": self.ha_version, "location_name": "PICO Lab"},
                }
            )
        elif msg_type == "ping":
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put({"id": cmd_id, "type": "pong"})
        elif msg_type == "subscribe_events":
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put({"id": cmd_id, "type": "result", "success": True})
        elif msg_type == "unsubscribe_events":
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put({"id": cmd_id, "type": "result", "success": True})
        else:
            cmd_id = payload.get("id", 1)
            await self._incoming_queue.put(
                {
                    "id": cmd_id,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": f"Unsupported command: {msg_type}"},
                }
            )

    async def receive_json(self, timeout: float = 10.0) -> dict[str, Any]:
        if not self._connected:
            raise ConnectionError("Mock WebSocket not connected.")
        try:
            return await asyncio.wait_for(self._incoming_queue.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            raise ConnectionError(f"Mock WebSocket receive timed out after {timeout}s.")

    async def close(self) -> None:
        self._connected = False
        self._authenticated = False


class HomeAssistantWebSocketClient:
    """Authenticated Home Assistant WebSocket API client with strict read-only enforcement."""

    def __init__(
        self,
        base_url: str,
        access_token: str | None,
        timeout_seconds: float = 10.0,
        transport: BaseWebSocketTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_token = access_token
        self._timeout = timeout_seconds
        self._transport = transport or AsyncioWebSocketTransport()
        self._cmd_counter = 0
        self._active_subscriptions: dict[int, asyncio.Task[None]] = {}

    def _get_ws_url(self) -> str:
        parsed = urlparse(self._base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        host = parsed.netloc or "127.0.0.1:8123"
        return f"{scheme}://{host}/api/websocket"

    async def connect_and_authenticate(self) -> dict[str, Any]:
        """Establish WebSocket connection and complete authentication handshake."""
        if not self._access_token or not self._access_token.strip():
            raise AuthorizationError("Missing Home Assistant access token for WebSocket authentication.")

        ws_url = self._get_ws_url()
        await self._transport.connect(ws_url, timeout=self._timeout)

        # 1. Expect auth_required
        msg = await self._transport.receive_json(timeout=self._timeout)
        if msg.get("type") != "auth_required":
            await self._transport.close()
            raise ConnectionError(f"Unexpected initial message from Home Assistant: {msg.get('type')}")

        # 2. Send auth
        await self._transport.send_json(
            {"type": "auth", "access_token": self._access_token}
        )

        # 3. Expect auth_ok or auth_invalid
        auth_resp = await self._transport.receive_json(timeout=self._timeout)
        if auth_resp.get("type") == "auth_invalid":
            await self._transport.close()
            raise AuthorizationError("Home Assistant rejected WebSocket access token (auth_invalid).")
        elif auth_resp.get("type") != "auth_ok":
            await self._transport.close()
            raise ConnectionError(f"WebSocket authentication failed: {auth_resp.get('message', 'unknown error')}")

        return auth_resp

    async def send_readonly_command(self, command_type: str, params: dict[str, Any] | None = None) -> Any:
        """Send an explicitly authorized read-only command and return the correlated result."""
        # Non-negotiable invariant: strictly reject any non-whitelisted or mutation command
        if command_type not in ALLOWED_READONLY_WS_COMMANDS:
            raise ValidationError(
                f"WebSocket command '{command_type}' is prohibited. Only read-only commands {ALLOWED_READONLY_WS_COMMANDS} are authorized."
            )

        if not self._transport.is_connected():
            await self.connect_and_authenticate()

        self._cmd_counter += 1
        cmd_id = self._cmd_counter

        payload: dict[str, Any] = {"id": cmd_id, "type": command_type}
        if params:
            payload.update(params)

        await self._transport.send_json(payload)

        # Correlate response by command id
        while True:
            resp = await self._transport.receive_json(timeout=self._timeout)
            if resp.get("id") == cmd_id:
                if resp.get("success") is False:
                    err = resp.get("error", {})
                    raise ConnectionError(f"Command '{command_type}' failed: {err.get('message', 'Unknown error')}")
                return resp.get("result")
            elif resp.get("type") == "pong" and command_type == "ping":
                return True

    async def get_states_via_websocket(self) -> list[dict[str, Any]]:
        result = await self.send_readonly_command("get_states")
        if isinstance(result, list):
            return result
        return []

    async def get_config_via_websocket(self) -> dict[str, Any]:
        result = await self.send_readonly_command("get_config")
        if isinstance(result, dict):
            return result
        return {}

    async def close(self) -> None:
        for task in self._active_subscriptions.values():
            task.cancel()
        self._active_subscriptions.clear()
        await self._transport.close()
