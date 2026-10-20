import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any
import httpx

from pico.providers.base import ModelProvider
from pico.providers.errors import (
    ProviderCancelledError,
    ProviderConnectionError,
    ProviderModelNotFoundError,
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


class OllamaProvider(ModelProvider):
    """Native Ollama provider adapter using asynchronous HTTP streaming."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model_name: str = "qwen3.5:2b",
        context_window: int = 4096,
        timeout: float = 60.0,
        think: bool | None = False,
        http_client: httpx.AsyncClient | None = None,
        default_max_tokens: int = 512,
    ) -> None:
        """Initialize OllamaProvider.

        Args:
            base_url: Ollama daemon base URL.
            model_name: Target model tag (e.g. 'qwen3.5:2b').
            context_window: Active context token limit.
            timeout: HTTP request timeout in seconds.
            think: Provider-level default for model thinking/reasoning process.
                Defaults to False for fast, bounded routine local inference.
                Precedence contract:
                - GenerationOptions.think when not None.
                - Otherwise, this provider default when configured (not None).
                - Otherwise, omit the 'think' field from the payload.
            http_client: Optional injected httpx client (e.g. for offline testing).
            default_max_tokens: Bound on generated completion tokens (num_predict).
                Defaults to 512 tokens to enforce context budget boundary.
        """
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.context_window = context_window
        self.timeout = timeout
        self.think = think
        self._http_client = http_client
        self.default_max_tokens = default_max_tokens
        self._metadata = ProviderMetadata(
            name="ollama",
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
        """Check if Ollama service is reachable and responding."""
        try:
            client = self._http_client or httpx.AsyncClient(timeout=5.0)
            try:
                res = await client.get(f"{self.base_url}/api/tags")
                return res.status_code == 200
            finally:
                if self._http_client is None:
                    await client.aclose()
        except Exception:
            return False

    async def stream_chat(
        self,
        messages: list[ChatMessage],
        options: GenerationOptions | None = None,
    ) -> AsyncIterator[StreamEvent]:
        start_time = time.perf_counter()

        # Build payload for Ollama native /api/chat endpoint
        ollama_messages = [
            {"role": m.role, "content": m.content}
            for m in messages
        ]

        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": ollama_messages,
            "stream": True,
            "options": {
                "num_ctx": self.context_window,
            },
        }

        # Resolve 'think' parameter per precedence contract:
        # 1. GenerationOptions.think when not None.
        # 2. Otherwise, OllamaProvider default (self.think) when configured (not None).
        # 3. Otherwise, omit the field.
        effective_think = (
            options.think
            if (options is not None and options.think is not None)
            else self.think
        )
        if effective_think is not None:
            payload["think"] = effective_think

        effective_max_tokens = (
            options.max_tokens
            if (options is not None and options.max_tokens is not None)
            else self.default_max_tokens
        )
        if effective_max_tokens is not None:
            payload["options"]["num_predict"] = effective_max_tokens

        if options:
            if options.temperature is not None:
                payload["options"]["temperature"] = options.temperature
            if options.top_p is not None:
                payload["options"]["top_p"] = options.top_p
            if options.json_schema is not None:
                payload["format"] = options.json_schema

        yield MetaEvent(
            provider="ollama",
            model=self.model_name,
            metadata={"num_ctx": self.context_window},
        )

        client = self._http_client or httpx.AsyncClient(timeout=self.timeout)
        should_close_client = self._http_client is None

        try:
            url = f"{self.base_url}/api/chat"
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code == 404:
                    body = await response.aread()
                    error_msg = body.decode("utf-8", errors="replace")
                    raise ProviderModelNotFoundError(
                        f"Ollama model '{self.model_name}' not found. Run 'ollama pull {self.model_name}' to install it. Details: {error_msg}"
                    )
                elif response.status_code != 200:
                    body = await response.aread()
                    raise ProviderResponseError(
                        f"Ollama returned HTTP {response.status_code}: {body.decode('utf-8', errors='replace')}"
                    )

                prompt_eval_count = None
                eval_count = None

                async for line in response.aiter_lines():
                    if not line or not line.strip():
                        continue

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProviderResponseError(f"Malformed NDJSON chunk from Ollama: {line}") from exc

                    if "error" in data:
                        error_text = data["error"]
                        if "not found" in error_text.lower():
                            raise ProviderModelNotFoundError(
                                f"Ollama model '{self.model_name}' not found: {error_text}"
                            )
                        raise ProviderResponseError(f"Ollama error: {error_text}")

                    # Stream text delta if present
                    msg = data.get("message", {})
                    content_chunk = msg.get("content", "")
                    if content_chunk:
                        yield TokenEvent(delta=content_chunk)

                    if data.get("done", False):
                        prompt_eval_count = data.get("prompt_eval_count")
                        eval_count = data.get("eval_count")
                        done_reason = data.get("done_reason", "stop")

                        native_meta: dict[str, Any] = {}
                        if "total_duration" in data and data["total_duration"] is not None:
                            native_meta["total_duration_ms"] = round(data["total_duration"] / 1e6, 2)
                        if "load_duration" in data and data["load_duration"] is not None:
                            native_meta["load_duration_ms"] = round(data["load_duration"] / 1e6, 2)
                        if "prompt_eval_duration" in data and data["prompt_eval_duration"] is not None:
                            native_meta["prompt_eval_duration_ms"] = round(data["prompt_eval_duration"] / 1e6, 2)
                        if "eval_duration" in data and data["eval_duration"] is not None:
                            native_meta["eval_duration_ms"] = round(data["eval_duration"] / 1e6, 2)

                        latency_ms = (time.perf_counter() - start_time) * 1000.0
                        total_tok = (
                            (prompt_eval_count or 0) + (eval_count or 0)
                            if prompt_eval_count is not None or eval_count is not None
                            else None
                        )

                        yield DoneEvent(
                            finish_reason=done_reason,
                            prompt_tokens=prompt_eval_count,
                            completion_tokens=eval_count,
                            total_tokens=total_tok,
                            latency_ms=round(latency_ms, 2),
                            metadata=native_meta,
                        )
                        return

        except httpx.ConnectError as exc:
            raise ProviderConnectionError(
                f"Ollama service unavailable at {self.base_url}. Ensure Ollama is installed and running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"Ollama request timed out after {self.timeout}s."
            ) from exc
        except asyncio.CancelledError:
            raise ProviderCancelledError("Ollama inference was cancelled")
        finally:
            if should_close_client:
                await client.aclose()
