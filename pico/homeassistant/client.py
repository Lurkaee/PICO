import json
import logging
import re
from typing import Any
from urllib.parse import urlparse
import httpx

from pico.core.config import settings
from pico.core.errors import (
    AuthorizationError,
    EntityNotFoundError,
    ValidationError,
)
from pico.homeassistant.contracts import (
    HomeAssistantConnectionError,
    HomeAssistantConnectionState,
    HomeAssistantStatus,
)
from pico.homeassistant.ws_client import (
    BaseWebSocketTransport,
    HomeAssistantWebSocketClient,
    is_loopback_host,
)

ConnectionError = HomeAssistantConnectionError

logger = logging.getLogger(__name__)

ENTITY_ID_REGEX = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")

# Byte limits for inbound HTTP responses before memory decoding
MAX_STATES_RESPONSE_BYTES = 2 * 1024 * 1024      # 2 MB for /api/states
MAX_SINGLE_ENTITY_RESPONSE_BYTES = 256 * 1024    # 256 KB for /api/states/{id}
MAX_API_STATUS_RESPONSE_BYTES = 64 * 1024        # 64 KB for /api/


class HomeAssistantClient:
    """Authenticated client for Home Assistant REST and WebSocket APIs.

    Enforces strict read-only discovery, bounded responses, and fail-closed privacy.
    """

    def __init__(
        self,
        base_url: str | None = None,
        access_token: str | None = None,
        timeout_seconds: float | None = None,
        enabled: bool | None = None,
        allow_insecure_transport: bool | None = None,
        ws_transport: BaseWebSocketTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._enabled = settings.HOME_ASSISTANT_ENABLED if enabled is None else enabled
        self._allow_insecure = (
            allow_insecure_transport
            if allow_insecure_transport is not None
            else settings.HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT
        )
        raw_url = (base_url or settings.HOME_ASSISTANT_BASE_URL).strip().rstrip("/")
        self._base_url = self._validate_and_sanitize_url(
            raw_url, allow_insecure_transport=self._allow_insecure
        )
        self._access_token = access_token if access_token is not None else settings.HOME_ASSISTANT_ACCESS_TOKEN
        self._timeout = timeout_seconds if timeout_seconds is not None else settings.HOME_ASSISTANT_TIMEOUT_SECONDS
        self._http_client = http_client
        self._ws_client = HomeAssistantWebSocketClient(
            base_url=self._base_url,
            access_token=self._access_token,
            timeout_seconds=self._timeout,
            transport=ws_transport,
        )

    @staticmethod
    def _validate_and_sanitize_url(url: str, allow_insecure_transport: bool = False) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValidationError(f"Invalid Home Assistant base URL scheme '{scheme}'. Must be http or https.")
        if not parsed.hostname:
            raise ValidationError(f"Invalid Home Assistant base URL '{url}'. Missing host.")

        # Reject query parameters and fragments that could alter target or confuse URL resolution
        if parsed.query or parsed.fragment:
            raise ValidationError(f"Invalid Home Assistant base URL '{url}'. Query parameters and fragments are prohibited.")

        # Validate port
        port = parsed.port
        if port is not None and (port < 1 or port > 65535):
            raise ValidationError(f"Invalid Home Assistant port {port}. Must be between 1 and 65535.")

        host = parsed.hostname

        # Enforce transport security policy
        if scheme == "http" and not is_loopback_host(host):
            if not allow_insecure_transport:
                raise ValidationError(
                    f"Insecure plaintext transport ('http://') to non-loopback host '{host}' is prohibited. "
                    f"Use 'https://' or set HOME_ASSISTANT_ALLOW_INSECURE_TRANSPORT=true."
                )
            logger.warning("INSECURE TRANSPORT: Plaintext Home Assistant HTTP connection allowed to non-loopback host by operator opt-in.")

        clean_netloc = host
        if port:
            clean_netloc = f"{clean_netloc}:{port}"
        return f"{scheme}://{clean_netloc}"

    def _get_headers(self) -> dict[str, str]:
        if not self._access_token or not self._access_token.strip():
            raise AuthorizationError("Missing Home Assistant access token in configuration.")
        return {
            "Authorization": f"Bearer {self._access_token.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client:
            return self._http_client
        return httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)

    async def _read_bounded_json_response(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        headers: dict[str, str],
        max_bytes: int,
        expected_type: type,
    ) -> Any:
        """Stream HTTP response with strict byte-bounds before parsing JSON into memory."""
        try:
            req = client.build_request(method, url, headers=headers)
            resp = await client.send(req, stream=True)
            try:
                if resp.status_code == 404:
                    clean_id = url.split("/")[-1]
                    raise EntityNotFoundError("HomeAssistantEntity", clean_id)
                if resp.status_code in (401, 403):
                    raise AuthorizationError("Home Assistant rejected the access token (401 Unauthorized).")
                if resp.status_code >= 400:
                    raise ConnectionError(f"Home Assistant returned HTTP error: {resp.status_code}")

                # Pre-check Content-Length header before reading payload
                cl_header = resp.headers.get("content-length")
                if cl_header:
                    try:
                        content_length = int(cl_header)
                        if content_length > max_bytes:
                            raise ValidationError(
                                f"Home Assistant response Content-Length ({content_length} bytes) exceeds maximum allowed limit of {max_bytes} bytes."
                            )
                    except ValueError:
                        pass

                chunks: list[bytes] = []
                total_bytes = 0
                async for chunk in resp.aiter_bytes():
                    total_bytes += len(chunk)
                    if total_bytes > max_bytes:
                        raise ValidationError(
                            f"Home Assistant response payload size exceeded maximum allowed limit of {max_bytes} bytes."
                        )
                    chunks.append(chunk)

                raw_body = b"".join(chunks)
                try:
                    decoded = raw_body.decode("utf-8")
                    data = json.loads(decoded)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValidationError(f"Malformed or truncated response from Home Assistant: {type(exc).__name__}")

                if not isinstance(data, expected_type):
                    raise ValidationError(
                        f"Unexpected response structure from Home Assistant. Expected {expected_type.__name__}, got {type(data).__name__}."
                    )

                return data
            finally:
                await resp.aclose()
        except httpx.TimeoutException:
            raise ConnectionError(f"Connection to Home Assistant timed out after {self._timeout}s.")
        except httpx.RequestError as exc:
            raise ConnectionError(f"Connection to Home Assistant failed: {type(exc).__name__}")

    async def get_status(self) -> HomeAssistantStatus:
        """Inspect and return verified integration and connection status."""
        if not self._enabled:
            return HomeAssistantStatus(
                enabled=False,
                configured=bool(self._access_token),
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.DISABLED,
                message="Home Assistant integration is disabled by default.",
                live_subscription_enabled=False,
                physical_device_connectivity_claimed=False,
            )

        if not self._access_token or not self._access_token.strip():
            return HomeAssistantStatus(
                enabled=True,
                configured=False,
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.MISSING_CONFIGURATION,
                message="Home Assistant access token is missing or empty.",
                live_subscription_enabled=False,
                physical_device_connectivity_claimed=False,
            )

        try:
            api_info = await self.check_api()
            return HomeAssistantStatus(
                enabled=True,
                configured=True,
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.CONNECTED,
                api_version=api_info.get("version"),
                message="Connected and responding.",
                live_subscription_enabled=settings.HOME_ASSISTANT_ALLOW_LIVE_SUBSCRIPTION,
                physical_device_connectivity_claimed=False,
            )
        except AuthorizationError as exc:
            return HomeAssistantStatus(
                enabled=True,
                configured=True,
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.AUTHENTICATION_REJECTED,
                message=str(exc),
                live_subscription_enabled=False,
                physical_device_connectivity_claimed=False,
            )
        except ConnectionError as exc:
            return HomeAssistantStatus(
                enabled=True,
                configured=True,
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.CONNECTION_UNAVAILABLE,
                message=str(exc),
                live_subscription_enabled=False,
                physical_device_connectivity_claimed=False,
            )
        except Exception as exc:
            return HomeAssistantStatus(
                enabled=True,
                configured=True,
                base_url=self._base_url,
                connection_state=HomeAssistantConnectionState.READ_FAILED,
                message=f"Failed to read Home Assistant status: {str(exc)}",
                live_subscription_enabled=False,
                physical_device_connectivity_claimed=False,
            )

    async def check_api(self) -> dict[str, Any]:
        """Perform authenticated REST API health check against GET /api/."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")

        url = f"{self._base_url}/api/"
        headers = self._get_headers()

        client = await self._get_http_client()
        try:
            return await self._read_bounded_json_response(
                client=client,
                method="GET",
                url=url,
                headers=headers,
                max_bytes=MAX_API_STATUS_RESPONSE_BYTES,
                expected_type=dict,
            )
        finally:
            if not self._http_client:
                await client.aclose()

    async def get_states(self) -> list[dict[str, Any]]:
        """Retrieve all reported entity states via REST GET /api/states with strict byte & count bounds."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")

        url = f"{self._base_url}/api/states"
        headers = self._get_headers()

        client = await self._get_http_client()
        try:
            data = await self._read_bounded_json_response(
                client=client,
                method="GET",
                url=url,
                headers=headers,
                max_bytes=MAX_STATES_RESPONSE_BYTES,
                expected_type=list,
            )
            # Validate each item has required entity_id
            for item in data:
                if not isinstance(item, dict) or "entity_id" not in item:
                    raise ValidationError("Malformed state object in Home Assistant response: missing 'entity_id'.")

            # Bounded entities limit to prevent memory exhaustion
            return data[: settings.HOME_ASSISTANT_MAX_ENTITIES_LIMIT]
        finally:
            if not self._http_client:
                await client.aclose()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """Retrieve reported state for a single entity via REST GET /api/states/{entity_id} with byte bound."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")

        clean_id = entity_id.strip()
        if not ENTITY_ID_REGEX.match(clean_id):
            raise ValidationError(
                f"Invalid entity_id format '{clean_id}'. Expected '<domain>.<object_id>' (e.g. 'light.kitchen')."
            )

        url = f"{self._base_url}/api/states/{clean_id}"
        headers = self._get_headers()

        client = await self._get_http_client()
        try:
            data = await self._read_bounded_json_response(
                client=client,
                method="GET",
                url=url,
                headers=headers,
                max_bytes=MAX_SINGLE_ENTITY_RESPONSE_BYTES,
                expected_type=dict,
            )
            if "entity_id" not in data:
                raise ValidationError(f"Malformed state response from Home Assistant for '{clean_id}': missing 'entity_id'.")
            return data
        finally:
            if not self._http_client:
                await client.aclose()

    # WebSocket Discovery
    async def get_states_ws(self) -> list[dict[str, Any]]:
        """Retrieve entity states using WebSocket transport."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")
        return await self._ws_client.get_states_via_websocket()

    async def get_config_ws(self) -> dict[str, Any]:
        """Retrieve basic system config using WebSocket transport."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")
        return await self._ws_client.get_config_via_websocket()

    async def close(self) -> None:
        await self._ws_client.close()
        if self._http_client:
            await self._http_client.aclose()
