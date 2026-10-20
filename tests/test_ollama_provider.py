import json
import httpx
import pytest

from pico.providers.errors import (
    ProviderConnectionError,
    ProviderModelNotFoundError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from pico.providers.ollama import OllamaProvider
from pico.providers.types import ChatMessage, DoneEvent, GenerationOptions, MetaEvent, TokenEvent


@pytest.mark.anyio
async def test_ollama_streaming_ndjson_success() -> None:
    chunks = [
        json.dumps({"model": "qwen3.5:2b", "message": {"role": "assistant", "content": "Local "}, "done": False}) + "\n",
        json.dumps({"model": "qwen3.5:2b", "message": {"role": "assistant", "content": "intelligence "}, "done": False}) + "\n",
        json.dumps({"model": "qwen3.5:2b", "message": {"role": "assistant", "content": "verified."}, "done": False}) + "\n",
        json.dumps({
            "model": "qwen3.5:2b",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 15,
            "eval_count": 3,
        }) + "\n",
    ]

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        req_data = json.loads(request.content.decode("utf-8"))
        assert req_data["model"] == "qwen3.5:2b"
        assert req_data["stream"] is True
        assert req_data["options"]["num_ctx"] == 4096
        assert req_data["options"]["num_predict"] == 512

        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "application/x-ndjson"},
            content="".join(chunks).encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model_name="qwen3.5:2b",
            http_client=http_client,
        )

        events = []
        tokens = []
        async for event in provider.stream_chat([ChatMessage(role="user", content="Ping")]):
            events.append(event)
            if isinstance(event, TokenEvent):
                tokens.append(event.delta)

        assert isinstance(events[0], MetaEvent)
        assert events[0].provider == "ollama"
        assert events[0].model == "qwen3.5:2b"

        assert "".join(tokens) == "Local intelligence verified."

        done = events[-1]
        assert isinstance(done, DoneEvent)
        assert done.finish_reason == "stop"
        assert done.prompt_tokens == 15
        assert done.completion_tokens == 3
        assert done.total_tokens == 18


@pytest.mark.anyio
async def test_ollama_model_not_found_404() -> None:
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=404,
            content=b'{"error": "model \'missing-model:latest\' not found"}',
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(
            model_name="missing-model:latest",
            http_client=http_client,
        )
        with pytest.raises(ProviderModelNotFoundError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
                pass
        assert "not found" in str(exc_info.value).lower()
        assert "ollama pull" in str(exc_info.value)


@pytest.mark.anyio
async def test_ollama_malformed_ndjson() -> None:
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            content=b"not a valid json chunk\n",
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(http_client=http_client)
        with pytest.raises(ProviderResponseError):
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
                pass


@pytest.mark.anyio
async def test_ollama_connection_failure() -> None:
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused to 127.0.0.1:11434")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(http_client=http_client)
        with pytest.raises(ProviderConnectionError) as exc_info:
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
                pass
        assert "service unavailable" in str(exc_info.value).lower()


@pytest.mark.anyio
async def test_ollama_timeout() -> None:
    async def mock_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Read timed out")

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(http_client=http_client)
        with pytest.raises(ProviderTimeoutError):
            async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
                pass


@pytest.mark.anyio
async def test_ollama_health_check() -> None:
    async def mock_healthy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, content=b'{"models": []}')

    transport_ok = httpx.MockTransport(mock_healthy)
    async with httpx.AsyncClient(transport=transport_ok) as client_ok:
        provider_ok = OllamaProvider(http_client=client_ok)
        assert await provider_ok.check_health() is True

    async def mock_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Down")

    transport_down = httpx.MockTransport(mock_down)
    async with httpx.AsyncClient(transport=transport_down) as client_down:
        provider_down = OllamaProvider(http_client=client_down)
        assert await provider_down.check_health() is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "provider_think,options_think,expected_in_payload,expected_value",
    [
        # With provider default configured (think=False)
        (False, None, True, False),       # Options None -> provider default (False)
        (False, True, True, True),        # Options True -> overrides provider default
        (False, False, True, False),      # Options False -> explicit False
        # With provider default configured (think=True)
        (True, None, True, True),         # Options None -> provider default (True)
        (True, False, True, False),       # Options False -> overrides provider default
        (True, True, True, True),         # Options True -> explicit True
        # Without provider default (think=None)
        (None, None, False, None),        # Options None -> omitted from payload
        (None, True, True, True),         # Options True -> explicit True
        (None, False, True, False),       # Options False -> explicit False
    ],
)
async def test_ollama_think_precedence_and_payload_serialization(
    provider_think: bool | None,
    options_think: bool | None,
    expected_in_payload: bool,
    expected_value: bool | None,
) -> None:
    captured_payload = None

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content.decode("utf-8"))
        done_chunk = json.dumps({"model": "qwen3.5:2b", "done": True, "done_reason": "stop"}) + "\n"
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "application/x-ndjson"},
            content=done_chunk.encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(
            think=provider_think,
            http_client=http_client,
        )
        options = GenerationOptions(think=options_think)
        async for _ in provider.stream_chat(
            [ChatMessage(role="user", content="Test")],
            options=options,
        ):
            pass

    assert captured_payload is not None
    if expected_in_payload:
        assert "think" in captured_payload
        assert captured_payload["think"] is expected_value
    else:
        assert "think" not in captured_payload


@pytest.mark.anyio
async def test_ollama_think_omitted_when_no_options_and_no_provider_default() -> None:
    captured_payload = None

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content.decode("utf-8"))
        done_chunk = json.dumps({"model": "qwen3.5:2b", "done": True, "done_reason": "stop"}) + "\n"
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "application/x-ndjson"},
            content=done_chunk.encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(
            think=None,
            http_client=http_client,
        )
        # Calling without options object at all
        async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass

    assert captured_payload is not None
    assert "think" not in captured_payload


@pytest.mark.anyio
async def test_mock_and_cloud_providers_tolerate_think_option() -> None:
    from pico.providers.cloud import CloudProvider
    from pico.providers.mock import MockModelProvider

    # MockModelProvider tolerates GenerationOptions with think=True / think=False
    mock_prov = MockModelProvider(fixed_response="mock response")
    events = []
    async for e in mock_prov.stream_chat(
        [ChatMessage(role="user", content="Test")],
        options=GenerationOptions(think=False),
    ):
        events.append(e)
    assert len(events) >= 2

    async for e in mock_prov.stream_chat(
        [ChatMessage(role="user", content="Test")],
        options=GenerationOptions(think=True),
    ):
        events.append(e)

    # CloudProvider tolerates GenerationOptions with think=True / think=False without error
    captured_cloud_payload = None

    async def cloud_mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_cloud_payload
        captured_cloud_payload = json.loads(request.content.decode("utf-8"))
        sse_chunk = 'data: {"id":"chat-1","choices":[{"delta":{"content":"cloud ok"}}],"usage":null}\n\ndata: [DONE]\n\n'
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/event-stream"},
            content=sse_chunk.encode("utf-8"),
        )

    cloud_transport = httpx.MockTransport(cloud_mock_handler)
    async with httpx.AsyncClient(transport=cloud_transport) as http_client:
        cloud_prov = CloudProvider(
            base_url="https://api.openai.com/v1",
            api_key="test-key",
            enabled=True,
            http_client=http_client,
        )
        async for _ in cloud_prov.stream_chat(
            [ChatMessage(role="user", content="Test", data_classification="PUBLIC")],
            options=GenerationOptions(think=False),
        ):
            pass

    assert captured_cloud_payload is not None
    # Cloud provider ignores think and does not forward it into standard completions payload
    assert "think" not in captured_cloud_payload


@pytest.mark.anyio
async def test_ollama_completion_limit_enforced_at_boundary() -> None:
    """Verify that num_predict and num_ctx are strictly passed to Ollama options at provider boundary."""
    captured_payload = None

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_payload
        captured_payload = json.loads(request.content.decode("utf-8"))
        done_chunk = json.dumps({
            "model": "qwen3.5:2b",
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 5,
        }) + "\n"
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "application/x-ndjson"},
            content=done_chunk.encode("utf-8"),
        )

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        provider = OllamaProvider(
            base_url="http://127.0.0.1:11434",
            model_name="qwen3.5:2b",
            context_window=4096,
            default_max_tokens=512,
            http_client=http_client,
        )

        # 1. Default options: num_ctx=4096, num_predict=512
        async for _ in provider.stream_chat([ChatMessage(role="user", content="Ping")]):
            pass
        assert captured_payload is not None
        assert captured_payload["options"]["num_ctx"] == 4096
        assert captured_payload["options"]["num_predict"] == 512

        # 2. Explicit max_tokens option: num_predict clamped/set to 256
        async for _ in provider.stream_chat(
            [ChatMessage(role="user", content="Ping")],
            options=GenerationOptions(max_tokens=256),
        ):
            pass
        assert captured_payload["options"]["num_ctx"] == 4096
        assert captured_payload["options"]["num_predict"] == 256
