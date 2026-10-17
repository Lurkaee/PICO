import asyncio
import pytest

from pico.providers.errors import (
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
)
from pico.providers.mock import MockModelProvider
from pico.providers.types import (
    ChatMessage,
    DoneEvent,
    MetaEvent,
    TokenEvent,
)


@pytest.mark.anyio
async def test_mock_provider_streaming_success() -> None:
    provider = MockModelProvider(fixed_response="Hello world from PICO mock.")
    messages = [ChatMessage(role="user", content="Ping")]

    events = []
    tokens = []
    async for event in provider.stream_chat(messages):
        events.append(event)
        if isinstance(event, TokenEvent):
            tokens.append(event.delta)

    # Verify event types and sequence
    assert isinstance(events[0], MetaEvent)
    assert events[0].provider == "mock"
    assert events[0].model == "pico-mock-deterministic"

    # Verify reconstructed text
    full_text = "".join(tokens)
    assert full_text == "Hello world from PICO mock."

    # Verify DoneEvent
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.finish_reason == "stop"
    assert done.prompt_tokens is not None and done.prompt_tokens > 0
    assert done.completion_tokens is not None and done.completion_tokens > 0
    assert done.latency_ms is not None and done.latency_ms >= 0


@pytest.mark.anyio
async def test_mock_provider_simulated_errors() -> None:
    # Connection error
    conn_provider = MockModelProvider(simulate_error="connection")
    with pytest.raises(ProviderConnectionError):
        async for _ in conn_provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass

    # Timeout error
    timeout_provider = MockModelProvider(simulate_error="timeout")
    with pytest.raises(ProviderTimeoutError):
        async for _ in timeout_provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass

    # Generic error
    generic_provider = MockModelProvider(simulate_error="generic")
    with pytest.raises(ProviderError):
        async for _ in generic_provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass


@pytest.mark.anyio
async def test_mock_provider_health_check() -> None:
    healthy_provider = MockModelProvider()
    assert await healthy_provider.check_health() is True

    unhealthy_provider = MockModelProvider(simulate_error="connection")
    assert await unhealthy_provider.check_health() is False


@pytest.mark.anyio
async def test_mock_provider_cancellation() -> None:
    provider = MockModelProvider(simulate_delay=0.5)

    async def run_stream():
        async for _ in provider.stream_chat([ChatMessage(role="user", content="Test")]):
            pass

    task = asyncio.create_task(run_stream())
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises((asyncio.CancelledError, ProviderCancelledError)):
        await task
