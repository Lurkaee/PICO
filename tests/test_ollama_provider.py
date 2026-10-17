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
from pico.providers.types import ChatMessage, DoneEvent, MetaEvent, TokenEvent


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
