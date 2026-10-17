from typing import Any
from pico.core.errors import PicoError


class ProviderError(PicoError):
    """Base exception for all model provider errors."""

    def __init__(self, message: str, code: str = "PROVIDER_ERROR", details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.code = code


class ProviderConfigurationError(ProviderError):
    """Raised when provider configuration or credentials are missing or invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_CONFIG_ERROR", details=details)


class ProviderConnectionError(ProviderError):
    """Raised when connection to provider endpoint fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_CONNECTION_ERROR", details=details)


class ProviderTimeoutError(ProviderError):
    """Raised when provider request exceeds configured timeout."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_TIMEOUT_ERROR", details=details)


class ProviderModelNotFoundError(ProviderError):
    """Raised when requested model is not found in provider runtime."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_MODEL_NOT_FOUND", details=details)


class ProviderResponseError(ProviderError):
    """Raised when provider returns an unparseable or malformed response."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_RESPONSE_ERROR", details=details)


class ProviderUnsupportedCapabilityError(ProviderError):
    """Raised when an operation requires an unsupported provider capability."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, code="PROVIDER_UNSUPPORTED_CAPABILITY", details=details)


import asyncio


class ProviderCancelledError(ProviderError, asyncio.CancelledError):
    """Raised when inference operation is cancelled by the caller."""

    def __init__(self, message: str = "Inference was cancelled", details: dict[str, Any] | None = None) -> None:
        ProviderError.__init__(self, message, code="PROVIDER_CANCELLED", details=details)
        asyncio.CancelledError.__init__(self, message)
