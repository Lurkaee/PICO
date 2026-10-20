import pytest
import httpx

from pico.core.errors import (
    AuthorizationError,
    EntityNotFoundError,
    ValidationError,
)
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    HomeAssistantConnectionError,
    HomeAssistantConnectionState,
)

ConnectionError = HomeAssistantConnectionError


@pytest.mark.anyio
async def test_rest_check_api_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/"
        assert request.headers.get("Authorization") == "Bearer test_token"
        return httpx.Response(200, json={"message": "API running."})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="test_token",
            http_client=http_client,
        )
        api_data = await client.check_api()
        assert api_data["message"] == "API running."

        status = await client.get_status()
        assert status.connection_state == HomeAssistantConnectionState.CONNECTED
        assert status.physical_device_connectivity_claimed is False


@pytest.mark.anyio
async def test_rest_check_api_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "401: Unauthorized"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="bad_token",
            http_client=http_client,
        )
        with pytest.raises(AuthorizationError) as exc:
            await client.check_api()
        assert "rejected" in str(exc.value).lower()

        status = await client.get_status()
        assert status.connection_state == HomeAssistantConnectionState.AUTHENTICATION_REJECTED


@pytest.mark.anyio
async def test_rest_check_api_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        with pytest.raises(ConnectionError) as exc:
            await client.check_api()
        assert "timed out" in str(exc.value).lower()

        status = await client.get_status()
        assert status.connection_state == HomeAssistantConnectionState.CONNECTION_UNAVAILABLE


@pytest.mark.anyio
async def test_rest_get_states_and_single_state() -> None:
    states_fixture = [
        {
            "entity_id": "light.living_room",
            "state": "on",
            "attributes": {"friendly_name": "Living Room Light", "brightness": 255},
            "last_changed": "2026-10-20T10:00:00Z",
            "last_updated": "2026-10-20T10:00:00Z",
        },
        {
            "entity_id": "sensor.kitchen_temperature",
            "state": "22.4",
            "attributes": {
                "friendly_name": "Kitchen Temperature",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
            },
            "last_changed": "2026-10-20T10:05:00Z",
            "last_updated": "2026-10-20T10:05:00Z",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states":
            return httpx.Response(200, json=states_fixture)
        elif request.url.path == "/api/states/light.living_room":
            return httpx.Response(200, json=states_fixture[0])
        elif request.url.path == "/api/states/sensor.nonexistent":
            return httpx.Response(404, json={"message": "Entity not found."})
        return httpx.Response(400)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="token",
            http_client=http_client,
        )
        states = await client.get_states()
        assert len(states) == 2
        assert states[0]["entity_id"] == "light.living_room"

        single = await client.get_state("light.living_room")
        assert single["state"] == "on"

        with pytest.raises(EntityNotFoundError):
            await client.get_state("sensor.nonexistent")

        # Invalid entity format validation
        with pytest.raises(ValidationError):
            await client.get_state("invalid_format_without_domain")
