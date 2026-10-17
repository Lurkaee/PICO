import asyncio
import time
from collections.abc import AsyncIterator

from pico.providers.base import ModelProvider
from pico.providers.errors import (
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderError,
    ProviderTimeoutError,
)
from pico.providers.types import (
    ChatMessage,
    DoneEvent,
    GenerationOptions,
    MetaEvent,
    ProviderCapability,
    ProviderMetadata,
    StreamEvent,
    TokenEvent,
)


class MockModelProvider(ModelProvider):
    """Deterministic, offline development mock provider."""

    def __init__(
        self,
        model_name: str = "pico-mock-deterministic",
        fixed_response: str | None = None,
        simulate_delay: float = 0.0,
        simulate_error: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.fixed_response = fixed_response
        self.simulate_delay = simulate_delay
        self.simulate_error = simulate_error
        self._metadata = ProviderMetadata(
            name="mock",
            default_model=self.model_name,
            capabilities=[
                ProviderCapability.STREAMING,
                ProviderCapability.JSON_SCHEMA,
                ProviderCapability.LOCAL_INFERENCE,
            ],
            is_local=True,
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    async def check_health(self) -> bool:
        if self.simulate_error == "connection":
            return False
        return True

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        start_time = time.perf_counter()

        # Check for simulated errors
        if self.simulate_error == "connection":
            raise ProviderConnectionError("Simulated mock connection failure")
        elif self.simulate_error == "timeout":
            raise ProviderTimeoutError("Simulated mock timeout")
        elif self.simulate_error == "generic":
            raise ProviderError("Simulated mock error")

        last_user_message = ""
        for m in reversed(messages):
            if m.role == "user":
                last_user_message = m.content
                break

        # Yield MetaEvent
        yield MetaEvent(
            provider="mock",
            model=self.model_name,
            metadata={"mock": True},
        )

        # Generate deterministic response content
        if self.fixed_response is not None:
            text = self.fixed_response
        else:
            text = f"[Mock Response] Received message: '{last_user_message}'. System is operating normally."

        # Tokenize into words with spaces for realistic streaming chunks
        tokens = text.split(" ")
        for i, word in enumerate(tokens):
            if self.simulate_delay > 0:
                try:
                    await asyncio.sleep(self.simulate_delay)
                except asyncio.CancelledError:
                    raise ProviderCancelledError("Mock streaming cancelled")

            chunk = word if i == 0 else " " + word
            yield TokenEvent(delta=chunk)

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        prompt_tokens = sum(max(1, len(m.content) // 4) for m in messages)
        completion_tokens = max(1, len(text) // 4)

        yield DoneEvent(
            finish_reason="stop",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=round(latency_ms, 2),
        )
