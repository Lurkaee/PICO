from pico.core.config import settings
from pico.providers.base import ModelProvider
from pico.providers.cloud import CloudProvider
from pico.providers.errors import ProviderConfigurationError
from pico.providers.mock import MockModelProvider
from pico.providers.ollama import OllamaProvider


class ProviderFactory:
    """Factory creating and resolving configured ModelProvider instances."""

    _instances: dict[str, ModelProvider] = {}

    @classmethod
    def get_provider(
        cls,
        provider_name: str | None = None,
        model_name: str | None = None,
        override_instance: ModelProvider | None = None,
    ) -> ModelProvider:
        if override_instance is not None:
            return override_instance

        target = (provider_name or settings.DEFAULT_PROVIDER).lower()

        if target == "mock":
            return MockModelProvider(model_name=model_name or "pico-mock-deterministic")
        elif target == "ollama":
            return OllamaProvider(
                base_url=settings.OLLAMA_BASE_URL,
                model_name=model_name or settings.OLLAMA_MODEL,
                context_window=settings.OLLAMA_CONTEXT_WINDOW,
                timeout=settings.OLLAMA_TIMEOUT_SECONDS,
            )
        elif target == "cloud":
            return CloudProvider(
                base_url=settings.CLOUD_BASE_URL,
                model_name=model_name or settings.CLOUD_MODEL,
                api_key=settings.CLOUD_API_KEY,
                enabled=settings.CLOUD_ENABLED,
                timeout=settings.CLOUD_TIMEOUT_SECONDS,
            )
        else:
            raise ProviderConfigurationError(f"Unknown or unsupported provider '{target}'")
