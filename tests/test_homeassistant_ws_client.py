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


@pytest.mark.anyio
async def test_ws_client_send_readonly_command_rejects_mutation_payload_overwrite() -> None:
    """Mandatory P0 test: Attempting to supply 'type': 'call_service' must fail closed before transmission."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    # Attempt type override
    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command(
            "get_states",
            {"type": "call_service", "domain": "light", "service": "turn_on"},
        )
    assert "reserved" in str(exc.value).lower()

    # Verify that the transport received ZERO outgoing messages with 'call_service'
    for sent in transport._last_sent:
        assert sent.get("type") != "call_service"

    await client.close()


@pytest.mark.anyio
async def test_ws_client_send_readonly_command_rejects_id_overwrite() -> None:
    """Mandatory P0 test: Attempting to supply a caller-controlled 'id' must fail closed."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command("get_states", {"id": 9999})
    assert "reserved" in str(exc.value).lower()

    # Verify no message sent with id 9999
    for sent in transport._last_sent:
        assert sent.get("id") != 9999

    await client.close()


@pytest.mark.anyio
async def test_ws_client_send_readonly_command_rejects_reserved_auth_fields() -> None:
    """Mandatory P0 test: Supplying authentication or privileged fields must fail closed."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command("get_states", {"access_token": "evil_token"})
    assert "reserved" in str(exc.value).lower()

    with pytest.raises(ValidationError) as exc2:
        await client.send_readonly_command("get_states", {"api_password": "evil_password"})
    assert "reserved" in str(exc2.value).lower()

    await client.close()


@pytest.mark.anyio
async def test_ws_client_send_readonly_command_rejects_extra_parameters() -> None:
    """Mandatory P0 test: Supplying extra parameters to commands that accept no parameters must fail closed."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command("get_states", {"extra_param": "forbidden"})
    assert "accepts no parameters" in str(exc.value).lower()

    with pytest.raises(ValidationError) as exc2:
        await client.send_readonly_command("ping", {"foo": "bar"})
    assert "accepts no parameters" in str(exc2.value).lower()

    await client.close()


@pytest.mark.anyio
async def test_ws_client_event_subscriptions_prohibited_in_read_only_phase() -> None:
    """subscribe_events and unsubscribe_events must be prohibited."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    with pytest.raises(ValidationError) as exc:
        await client.send_readonly_command("subscribe_events")
    assert "prohibited" in str(exc.value).lower()

    with pytest.raises(ValidationError) as exc2:
        await client.send_readonly_command("unsubscribe_events")
    assert "prohibited" in str(exc2.value).lower()

    await client.close()


@pytest.mark.anyio
async def test_ws_client_helper_methods_enforce_clean_payloads() -> None:
    """Ensure all helper methods use guarded transport and produce clean payloads."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    states = await client.get_states_via_websocket()
    assert len(states) == 2
    last_cmd = transport._last_sent[-1]
    assert last_cmd == {"id": 1, "type": "get_states"}

    config = await client.get_config_via_websocket()
    assert config["location_name"] == "PICO Lab"
    last_cmd2 = transport._last_sent[-1]
    assert last_cmd2 == {"id": 2, "type": "get_config"}

    await client.close()


@pytest.mark.anyio
async def test_ws_client_unsolicited_events_ignored_during_correlation() -> None:
    """Unsolicited event broadcasts must not be confused with command results."""
    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    await client.connect_and_authenticate()

    # Enqueue an unsolicited event and a mismatched ID message ahead of the real response
    await transport._incoming_queue.put({"type": "event", "event": {"data": "noise"}})
    await transport._incoming_queue.put({"id": 9999, "type": "result", "result": "noise"})

    result = await client.send_readonly_command("ping")
    assert result is True

    await client.close()



@pytest.mark.anyio
async def test_ws_client_concurrent_serialization() -> None:
    """Multiple concurrent calls are serialized safely under asyncio.Lock()."""
    import asyncio

    transport = MockWebSocketTransport(valid_token="secret_token")
    client = HomeAssistantWebSocketClient(
        base_url="http://127.0.0.1:8123",
        access_token="secret_token",
        transport=transport,
    )

    results = await asyncio.gather(
        client.get_states_via_websocket(),
        client.get_config_via_websocket(),
        client.send_readonly_command("ping"),
    )
    assert len(results[0]) == 2
    assert results[1]["location_name"] == "PICO Lab"
    assert results[2] is True

    await client.close()
