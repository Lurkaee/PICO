import pytest
from fastapi.testclient import TestClient
import httpx

from pico.main import app
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.service import HomeAssistantService

client = TestClient(app)


def test_api_status_disabled_by_default() -> None:
    resp = client.get("/api/homeassistant/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["connection_state"] == "disabled"
    assert data["physical_device_connectivity_claimed"] is False


def test_api_entities_and_overview_with_mocked_client(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_states = [
        {
            "entity_id": "light.living_room",
            "state": "on",
            "attributes": {
                "friendly_name": "Living Room Ceiling",
                "brightness": 200,
                "latitude": 37.7749,  # Should be stripped!
                "longitude": -122.4194,  # Should be stripped!
                "access_token": "leak_candidate",  # Should be stripped!
            },
            "last_changed": "2026-10-20T12:00:00Z",
            "last_updated": "2026-10-20T12:00:00Z",
        },
        {
            "entity_id": "sensor.hallway_temp",
            "state": "20.5",
            "attributes": {
                "friendly_name": "Hallway Temperature",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
            },
            "last_changed": "2026-10-20T12:00:00Z",
            "last_updated": "2026-10-20T12:00:00Z",
        },
        {
            "entity_id": "lock.front_door",
            "state": "locked",
            "attributes": {
                "friendly_name": "Front Door Deadbolt",
            },
            "last_changed": "2026-10-20T12:00:00Z",
            "last_updated": "2026-10-20T12:00:00Z",
        },
    ]

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        elif request.url.path == "/api/states":
            return httpx.Response(200, json=mock_states)
        elif request.url.path == "/api/states/light.living_room":
            return httpx.Response(200, json=mock_states[0])
        elif request.url.path == "/api/states/lock.front_door":
            return httpx.Response(200, json=mock_states[2])
        return httpx.Response(404, json={"message": "Not Found"})

    transport = httpx.MockTransport(mock_handler)
    mock_http_client = httpx.AsyncClient(transport=transport)

    # Inject mocked client into service
    ha_client = HomeAssistantClient(
        enabled=True,
        base_url="http://127.0.0.1:8123",
        access_token="mock_token",
        http_client=mock_http_client,
    )
    monkeypatch.setattr(
        "pico.api.homeassistant.HomeAssistantService",
        lambda: HomeAssistantService(client=ha_client),
    )

    # 1. Test Status
    s_resp = client.get("/api/homeassistant/status")
    assert s_resp.status_code == 200
    s_data = s_resp.json()
    assert s_data["enabled"] is True
    assert s_data["connection_state"] == "connected"
    assert s_data["physical_device_connectivity_claimed"] is False

    # 2. Test List Entities
    e_resp = client.get("/api/homeassistant/entities")
    assert e_resp.status_code == 200
    e_data = e_resp.json()
    assert e_data["total_count"] == 3
    # Effective classification elevated to SENSITIVE because of lock.front_door
    assert e_data["data_classification"] == "SENSITIVE"

    # Verify stripped attributes on light
    light = next(e for e in e_data["entities"] if e["entity_id"] == "light.living_room")
    assert "latitude" not in light["attributes"]
    assert "longitude" not in light["attributes"]
    assert "access_token" not in light["attributes"]
    assert light["attributes"]["friendly_name"] == "Living Room Ceiling"

    # 3. Test Domain Filter
    filtered_resp = client.get("/api/homeassistant/entities?domain=sensor")
    assert filtered_resp.status_code == 200
    filtered_data = filtered_resp.json()
    assert filtered_data["total_count"] == 1
    assert filtered_data["entities"][0]["entity_id"] == "sensor.hallway_temp"
    assert filtered_data["data_classification"] == "INTERNAL"

    # 4. Test Single Entity State
    single_resp = client.get("/api/homeassistant/entities/lock.front_door")
    assert single_resp.status_code == 200
    single_data = single_resp.json()
    assert single_data["entity_id"] == "lock.front_door"
    assert single_data["data_classification"] == "SENSITIVE"
    assert single_data["state"] == "locked"

    # 5. Test Nonexistent Entity (404)
    nf_resp = client.get("/api/homeassistant/entities/light.nonexistent")
    assert nf_resp.status_code == 404

    # 6. Test Overview Endpoint
    ov_resp = client.get("/api/homeassistant/overview")
    assert ov_resp.status_code == 200
    ov_data = ov_resp.json()
    assert ov_data["total_entities"] == 3
    assert ov_data["domain_counts"]["light"] == 1
    assert ov_data["domain_counts"]["sensor"] == 1
    assert ov_data["domain_counts"]["lock"] == 1
    assert len(ov_data["highlights"]) == 3
