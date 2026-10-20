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
)

ConnectionError = HomeAssistantConnectionError

ENTITY_ID_REGEX = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")


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
        ws_transport: BaseWebSocketTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._enabled = settings.HOME_ASSISTANT_ENABLED if enabled is None else enabled
        raw_url = (base_url or settings.HOME_ASSISTANT_BASE_URL).strip().rstrip("/")
        self._base_url = self._validate_and_sanitize_url(raw_url)
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
    def _validate_and_sanitize_url(url: str) -> str:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            raise ValidationError(f"Invalid Home Assistant base URL scheme '{scheme}'. Must be http or https.")
        if not parsed.netloc:
            raise ValidationError(f"Invalid Home Assistant base URL '{url}'. Missing host.")
        # Strip userinfo or credentials from the URL
        clean_netloc = parsed.hostname or "127.0.0.1"
        if parsed.port:
            clean_netloc = f"{clean_netloc}:{parsed.port}"
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
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise AuthorizationError("Home Assistant rejected the access token (401 Unauthorized).")
            if resp.status_code >= 400:
                raise ConnectionError(f"Home Assistant API returned HTTP error: {resp.status_code}")
            return resp.json()
        except httpx.TimeoutException:
            raise ConnectionError(f"Connection to Home Assistant timed out after {self._timeout}s.")
        except httpx.RequestError as exc:
            raise ConnectionError(f"Connection to Home Assistant failed: {type(exc).__name__}")
        finally:
            if not self._http_client:
                await client.aclose()

    async def get_states(self) -> list[dict[str, Any]]:
        """Retrieve all reported entity states via REST GET /api/states."""
        if not self._enabled:
            raise ValidationError("Home Assistant integration is disabled by policy.")

        url = f"{self._base_url}/api/states"
        headers = self._get_headers()

        client = await self._get_http_client()
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code in (401, 403):
                raise AuthorizationError("Home Assistant rejected the access token (401 Unauthorized).")
            if resp.status_code >= 400:
                raise ConnectionError(f"Home Assistant returned HTTP {resp.status_code} while reading states.")

            data = resp.json()
            if not isinstance(data, list):
                raise ValidationError("Malformed response from Home Assistant: expected a list of state objects.")

            # Bounded entities limit to prevent memory exhaustion
            return data[: settings.HOME_ASSISTANT_MAX_ENTITIES_LIMIT]
        except httpx.TimeoutException:
            raise ConnectionError(f"Connection to Home Assistant timed out after {self._timeout}s.")
        except httpx.RequestError as exc:
            raise ConnectionError(f"Connection to Home Assistant failed: {type(exc).__name__}")
        finally:
            if not self._http_client:
                await client.aclose()

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        """Retrieve reported state for a single entity via REST GET /api/states/{entity_id}."""
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
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                raise EntityNotFoundError("HomeAssistantEntity", clean_id)
            if resp.status_code in (401, 403):
                raise AuthorizationError("Home Assistant rejected the access token (401 Unauthorized).")
            if resp.status_code >= 400:
                raise ConnectionError(f"Home Assistant returned HTTP {resp.status_code} for entity '{clean_id}'.")

            data = resp.json()
            if not isinstance(data, dict):
                raise ValidationError(f"Malformed response from Home Assistant for entity '{clean_id}'.")
            return data
        except httpx.TimeoutException:
            raise ConnectionError(f"Connection to Home Assistant timed out after {self._timeout}s.")
        except httpx.RequestError as exc:
            raise ConnectionError(f"Connection to Home Assistant failed: {type(exc).__name__}")
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
