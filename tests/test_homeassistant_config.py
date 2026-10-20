import pytest
import httpx
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import HomeAssistantConnectionState


def test_homeassistant_disabled_by_default() -> None:
    assert settings.HOME_ASSISTANT_ENABLED is False
    assert settings.HOME_ASSISTANT_ALLOW_LIVE_SUBSCRIPTION is False


@pytest.mark.anyio
async def test_homeassistant_disabled_status() -> None:
    client = HomeAssistantClient(enabled=False)
    status = await client.get_status()
    assert status.enabled is False
    assert status.connection_state == HomeAssistantConnectionState.DISABLED
    assert "disabled by default" in status.message.lower()
    assert status.physical_device_connectivity_claimed is False


@pytest.mark.anyio
async def test_homeassistant_missing_token_status() -> None:
    client = HomeAssistantClient(enabled=True, access_token=None)
    status = await client.get_status()
    assert status.enabled is True
    assert status.configured is False
    assert status.connection_state == HomeAssistantConnectionState.MISSING_CONFIGURATION
    assert status.physical_device_connectivity_claimed is False


def test_homeassistant_base_url_validation_and_sanitization() -> None:
    # Valid URLs
    c1 = HomeAssistantClient(base_url="http://192.168.1.100:8123")
    assert c1._base_url == "http://192.168.1.100:8123"

    c2 = HomeAssistantClient(base_url="https://homeassistant.local:8123/")
    assert c2._base_url == "https://homeassistant.local:8123"

    # Strips embedded credentials from URL
    c3 = HomeAssistantClient(base_url="http://admin:secret@127.0.0.1:8123")
    assert "secret" not in c3._base_url
    assert "admin" not in c3._base_url
    assert c3._base_url == "http://127.0.0.1:8123"

    # Rejects invalid schemes
    with pytest.raises(ValidationError):
        HomeAssistantClient(base_url="ftp://homeassistant.local:8123")

    with pytest.raises(ValidationError):
        HomeAssistantClient(base_url="javascript:alert(1)")


@pytest.mark.anyio
async def test_token_never_exposed_in_status_or_string() -> None:
    secret_token = "ultra_secret_ha_long_lived_token_xyz123"
    client = HomeAssistantClient(
        enabled=True,
        base_url="http://127.0.0.1:8123",
        access_token=secret_token,
    )
    status = await client.get_status()
    serialized = status.model_dump_json()

    assert secret_token not in serialized
    assert secret_token not in str(status)


@pytest.mark.anyio
async def test_redirects_do_not_follow_and_cannot_leak_token() -> None:
    # Mock transport returning 302 redirect
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=302,
            headers={"Location": "http://evil-external-host.com/steal"},
        )

    mock_transport = httpx.MockTransport(redirect_handler)
    async with httpx.AsyncClient(transport=mock_transport, follow_redirects=False) as http_client:
        client = HomeAssistantClient(
            enabled=True,
            base_url="http://127.0.0.1:8123",
            access_token="secret_token",
            http_client=http_client,
        )
        # Should not follow redirect and fail safely
        status = await client.get_status()
        assert status.connection_state in (
            HomeAssistantConnectionState.CONNECTION_UNAVAILABLE,
            HomeAssistantConnectionState.READ_FAILED,
        )
