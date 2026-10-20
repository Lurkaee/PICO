from typing import Any


class PicoError(Exception):
    """Base exception for all PICO domain errors."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class EntityNotFoundError(PicoError):
    """Raised when a requested resource is not found."""

    def __init__(self, entity_name: str, entity_id: str) -> None:
        super().__init__(
            f"{entity_name} with id '{entity_id}' does not exist.",
            details={"entity_name": entity_name, "entity_id": entity_id},
        )


class ValidationError(PicoError):
    """Raised when an entity violates business invariants."""

    code: str = "VALIDATION_ERROR"

    def __init__(self, message: str, field: str | None = None) -> None:
        details = {"field": field} if field else {}
        super().__init__(message, details=details)


class ConflictError(PicoError):
    """Raised when an operation conflicts with current state (e.g. uniqueness)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class DatabaseError(PicoError):
    """Raised when an unexpected database error occurs."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AuthorizationError(PicoError):
    """Raised when an operation requires human authorization or approval that was not granted."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)


class NotFoundError(EntityNotFoundError):
    """General not found error."""

    def __init__(self, message: str) -> None:
        super(PicoError, self).__init__(message)
        self.message = message
        self.details = {}
