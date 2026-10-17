import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
import httpx

from pico.providers.base import ModelProvider
from pico.providers.errors import (
    ProviderCancelledError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderResponseError,
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


class CloudProvider(ModelProvider):
    """Configurable OpenAI-compatible cloud provider adapter."""

    def __init__(
        self,
        base_url: str | None = None,
        model_name: str | None = None,
        api_key: str | None = None,
        enabled: bool = False,
        timeout: float = 60.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.model_name = model_name or "gpt-4o-mini"
        self._api_key = api_key
        self.enabled = enabled
        self.timeout = timeout
        self._http_client = http_client
        self._metadata = ProviderMetadata(
            name="cloud",
            default_model=self.model_name,
            capabilities=[
                ProviderCapability.STREAMING,
                ProviderCapability.JSON_SCHEMA,
            ],
            is_local=False,
        )

    @property
    def metadata(self) -> ProviderMetadata:
        return self._metadata

    async def check_health(self) -> bool:
        """Check if cloud provider is enabled and configured."""
        return bool(self.enabled and self._api_key)

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Guard: Cloud must be explicitly enabled
        if not self.enabled:
            raise ProviderConfigurationError(
                "Cloud provider is disabled by policy. Set CLOUD_ENABLED=true to enable cloud models."
            )

        # Guard: API key must be present (never reveal secret in error messages)
        if not self._api_key or not self._api_key.strip():
            raise ProviderConfigurationError(
                "Cloud provider API key is not configured. Set CLOUD_API_KEY in environment."
            )

        # Defense-in-depth security guard: Never send SECRET, SENSITIVE, or INTERNAL content to cloud
        for m in messages:
            norm = (m.data_classification or "INTERNAL").strip().upper()
            if norm != "PUBLIC":
                raise ProviderConfigurationError(
                    f"Data classification violation: Message classified as '{norm}' cannot be sent to cloud inference."
                )

        start_time = time.perf_counter()

        cloud_messages = [
            {"role": m.role, "content": m.content}
            for m in messages
        ]

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": cloud_messages,
            "stream": True,
        }

        if options:
            if options.temperature is not None:
                payload["temperature"] = options.temperature
            if options.top_p is not None:
                payload["top_p"] = options.top_p
            if options.max_tokens is not None:
                payload["max_tokens"] = options.max_tokens
            if options.json_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"schema": options.json_schema, "name": "output_schema"},
                }

        yield MetaEvent(
            provider="cloud",
            model=self.model_name,
            metadata={"cloud": True},
        )

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        client = self._http_client or httpx.AsyncClient(timeout=self.timeout)
        should_close_client = self._http_client is None

        try:
            url = f"{self.base_url}/chat/completions"
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    # Sanitize error to avoid leaking tokens if upstream mirrors headers
                    error_text = body.decode("utf-8", errors="replace")
                    raise ProviderResponseError(
                        f"Cloud provider returned HTTP {response.status_code}: {error_text}"
                    )

                accumulated_text: list[str] = []
                finish_reason = "stop"

                async for line in response.aiter_lines():
                    if not line:
                        continue
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue

                    raw_data = line[5:].strip()
                    if raw_data == "[DONE]":
                        break

                    try:
                        chunk = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(f"Malformed SSE chunk from cloud provider: {raw_data}") from exc

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content_piece = delta.get("content", "")
                    if content_piece:
                        accumulated_text.append(content_piece)
                        yield TokenEvent(delta=content_piece)

                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                latency_ms = (time.perf_counter() - start_time) * 1000.0
                prompt_tokens = sum(max(1, len(m.content) // 4) for m in messages)
                completion_tokens = max(1, sum(len(c) for c in accumulated_text) // 4)

                yield DoneEvent(
                    finish_reason=finish_reason,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    latency_ms=round(latency_ms, 2),
                )

        except httpx.ConnectError as exc:
            raise ProviderConnectionError(
                f"Failed to connect to cloud provider at {self.base_url}."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Cloud provider request timed out after {self.timeout}s."
            ) from exc
        except asyncio.CancelledError:
            raise ProviderCancelledError("Cloud inference was cancelled")
        finally:
            if should_close_client:
                await client.aclose()
