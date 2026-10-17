from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from pico.providers.types import (
    ChatMessage,
    DoneEvent,
    GenerationOptions,
    ProviderMetadata,
    StreamEvent,
    TokenEvent,
)


class ModelProvider(ABC):
    """Abstract base class for all PICO model providers."""

    @property
    @abstractmethod
    def metadata(self) -> ProviderMetadata:
        """Return provider metadata and declared capabilities."""
        ...

    @abstractmethod
    async def check_health(self) -> bool:
        """Verify whether the provider runtime is available and responding."""
        ...

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream conversational model inference tokens and lifecycle events."""
        ...

    async def complete_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> tuple[str, DoneEvent]:
        """Helper to collect a complete response from stream_chat."""
        collected: list[str] = []
        final_done = DoneEvent(finish_reason="stop")
        async for event in self.stream_chat(messages, options):
            if isinstance(event, TokenEvent):
                collected.append(event.delta)
            elif isinstance(event, DoneEvent):
                final_done = event
        return "".join(collected), final_done
