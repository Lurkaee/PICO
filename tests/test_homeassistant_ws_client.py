import pytest
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.contracts import HomeAssistantConnectionError

ConnectionError = HomeAssistantConnectionError
from pico.homeassistant.ws_client import (
    HomeAssistantWebSocketClient,
    MockWebSocketTransport,
)


@pytest.mark.anyio
async def test_ws_client_successful_auth_and_get_states() -> None:
    transport = MockWebSocketTransport(valid_token="secret_long_lived_token", ha_version="2026.3.0")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_long_lived_token",
        transport=transport,
    )

    states = await client.get_states_via_websocket()
    assert len(states) == 2
    assert states[0]["entity_id"] == "light.living_room"
    assert states[1]["entity_id"] == "sensor.office_temperature"

    config = await client.get_config_via_websocket()
    assert config["location_name"] == "PICO Lab"

    await client.close()


@pytest.mark.anyio
async def test_ws_client_auth_invalid() -> None:
    transport = MockWebSocketTransport(valid_token="correct_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="wrong_token",
        transport=transport,
    )

    with pytest.raises(AuthorizationError) as exc:
        await client.connect_and_authenticate()
    assert "rejected" in str(exc.value).lower()
    await client.close()


@pytest.mark.anyio
async def test_ws_client_prohibited_commands_rejected_before_transmission() -> None:
    transport = MockWebSocketTransport(valid_token="token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="token",
        transport=transport,
    )

    # Hard boundary: call_service is strictly prohibited
    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command("call_service", {"domain": "light", "service": "turn_on"})
    assert "prohibited" in str(exc.value).lower()

    # Generic command injection prohibited
    with pytest.raises(ValidationError):
        await client.send_readonly_command("shell_command", {})

    with pytest.raises(ValidationError):
        await client.send_readonly_command("fire_event", {})

    await client.close()


@pytest.mark.anyio
async def test_ws_client_missing_token_fails_closed() -> None:
    transport = MockWebSocketTransport()
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token=None,
        transport=transport,
    )
    with pytest.raises(AuthorizationError):
        await client.connect_and_authenticate()
