import json
import httpx
import pytest

from pico.providers.cloud import CloudProvider
from pico.providers.errors import (
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from pico.providers.types import ChatMessage, DoneEvent, MetaEvent, TokenEvent


@pytest.mark.anyio
async def test_cloud_provider_disabled_by_default() -> None:
    provider = CloudProvider(enabled=False, api_key="sk-test-secret-12345")
    with pytest.raises(ProviderConfigurationError) as exc_info:
        async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass
    assert "disabled by policy" in str(exc_info.value).lower()


@pytest.mark.anyio
async def test_cloud_provider_missing_api_key() -> None:
    provider = CloudProvider(enabled=True, api_key=None)
    with pytest.raises(ProviderConfigurationError) as exc_info:
        async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass
    assert "api key is not configured" in str(exc_info.value).lower()


@pytest.mark.anyio
async def test_cloud_provider_streaming_success() -> None:
    sse_lines = [
        "data: " + json.dumps({"choices": [{"delta": {"content": "Cloud "}, "finish_reason": None}]}) + "\n\n",
        "data: " + json.dumps({"choices": [{"delta": {"content": "reasoning "}, "finish_reason": None}]}) + "\n\n",
        "data: " + json.dumps({"choices": [{"delta": {"content": "streamed."}, "finish_reason": "stop"}]}) + "\n\n",
        "data: [DONE]\n\n",
    ]

    captured_headers = {}

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path in ("/v1/chat/completions", "/chat/completions")
        captured_headers.update(dict(request.headers))
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            content="".join(sse_lines).encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            base_url="https://api.openai.com/v1",
            model_name="gpt-4o-mini",
            api_key="sk-test-key-54321",
            enabled=True,
            http_client=http_client,
        )

        events = []
        tokens = []
        async for event in provider.stream_chat([ChatMessage(role="user", content="Analyze this", data_classification="PUBLIC")]):
            events.append(event)
            if isinstance(event, TokenEvent):
                tokens.append(event.delta)

        assert isinstance(events[0], MetaEvent)
        assert events[0].provider == "cloud"
        assert events[0].model == "gpt-4o-mini"

        assert "".join(tokens) == "Cloud reasoning streamed."

        done = events[-1]
        assert isinstance(done, DoneEvent)
        assert done.finish_reason == "stop"

        # Verify authorization header passed
        assert captured_headers.get("authorization") == "Bearer sk-test-key-54321"


@pytest.mark.anyio
async def test_cloud_provider_error_no_secret_leakage() -> None:
    secret_key = "sk-super-secret-key-99999"

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=401,
            content=b'{"error": {"message": "Invalid API Key"}}',
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            enabled=True,
            api_key=secret_key,
            http_client=http_client,
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass

        error_message = str(exc_info.value)
        # Ensure the secret key was never printed in the error message
        assert secret_key not in error_message
        assert "401" in error_message


@pytest.mark.anyio
async def test_cloud_provider_connection_failure() -> None:
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(enabled=True, api_key="sk-test", http_client=http_client)
        with pytest.raises(ProviderConnectionError):
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass


@pytest.mark.anyio
async def test_cloud_provider_rejects_non_public_messages_defense_in_depth() -> None:
    provider = CloudProvider(enabled=True, api_key="sk-test-key")

    for non_public in ["INTERNAL", "SENSITIVE", "SECRET", "UNKNOWN"]:
        msg = ChatMessage(role="user", content="Classified text", data_classification=non_public)
        with pytest.raises(ProviderConfigurationError) as exc_info:
            async for _ in provider.stream_chat([msg]):
                pass
        assert "cannot be sent to cloud" in str(exc_info.value)


@pytest.mark.anyio
async def test_cloud_provider_upstream_error_sanitization_canary_secret() -> None:
    canary_secret = "CANARY_SECRET_ALPHA_BRAVO_98765"
    raw_payload = {
        "error": {
            "message": f"Fatal token error with raw canary: {canary_secret}",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=400,
            content=json.dumps(raw_payload).encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            enabled=True,
            api_key="sk-client-cloud-key",
            http_client=http_client,
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass

        err_text = str(exc_info.value)
        assert canary_secret not in err_text
        assert "400" in err_text
        assert "invalid_request_error" in err_text
        assert "invalid_api_key" in err_text


@pytest.mark.anyio
async def test_cloud_provider_upstream_raw_body_canary_does_not_leak() -> None:
    canary_secret = "CANARY_RAW_HTML_CRASH_SECRET_77777"
    raw_content = f"<html><body>Internal server crash dump: {canary_secret}</body></html>".encode("utf-8")

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=500, content=raw_content)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            enabled=True,
            api_key="sk-client-cloud-key",
            http_client=http_client,
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass

        err_text = str(exc_info.value)
        assert canary_secret not in err_text
        assert "500" in err_text
        assert "Internal Server Error" in err_text


@pytest.mark.anyio
async def test_cloud_provider_malformed_chunk_canary_does_not_leak() -> None:
    canary_secret = "CANARY_CHUNK_SYNTAX_ERROR_66666"

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            content=f"data: {{unquoted_key: {canary_secret}\n\n".encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            enabled=True,
            api_key="sk-client-cloud-key",
            http_client=http_client,
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass

        err_text = str(exc_info.value)
        assert canary_secret not in err_text
        assert "Malformed SSE chunk received from cloud provider." in err_text


@pytest.mark.anyio
async def test_cloud_provider_streamed_error_event_canary_does_not_leak() -> None:
    canary_secret = "CANARY_STREAM_ERR_33333"
    streamed_err = {
        "error": {
            "message": f"Stream dropped mid-way with secret: {canary_secret}",
            "type": "server_error",
            "code": "overloaded",
        }
    }

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            content=f"data: {json.dumps(streamed_err)}\n\n".encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = CloudProvider(
            enabled=True,
            api_key="sk-client-cloud-key",
            http_client=http_client,
        )
        with pytest.raises(ProviderResponseError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test", data_classification="PUBLIC")]):
                pass

        err_text = str(exc_info.value)
        assert canary_secret not in err_text
        assert "Cloud provider streamed an error event" in err_text
        assert "server_error" in err_text
        assert "overloaded" in err_text
