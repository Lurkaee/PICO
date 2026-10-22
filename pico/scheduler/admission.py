import asyncio
from pico.core.config import settings
from pico.core.errors import ValidationError
from pico.scheduler.errors import ModelResidencyConflictError
from pico.scheduler.types import SchedulingRequest


class LocalAdmissionGate:
    """Enforces strict single-model residency and single-lease local inference concurrency."""

    def __init__(
        self,
        resident_model: str | None = None,
        max_active: int = 1,
    ) -> None:
        self.max_active = max(1, max_active)
        self.resident_model = resident_model or settings.OLLAMA_MODEL
        self.active_count = 0
        self._lock = asyncio.Lock()

    def validate_residency(self, request: SchedulingRequest) -> None:
        """Validate that the request does not attempt to instantiate a conflicting local model."""
        # For mock or ollama provider, model must match resident model
        req_model = request.model_name.strip()
        if settings.DEFAULT_PROVIDER == "mock":
            # In mock mode, permit mock model or resident
            if req_model not in ("pico-mock-deterministic", self.resident_model):
                raise ModelResidencyConflictError(
                    f"Requested local model '{req_model}' conflicts with resident model '{self.resident_model}'. "
                    f"Instantiating multiple local models is strictly prohibited.",
                    details={"requested_model": req_model, "resident_model": self.resident_model},
                )
        else:
            if req_model != self.resident_model:
                raise ModelResidencyConflictError(
                    f"Requested local model '{req_model}' conflicts with resident model '{self.resident_model}'. "
                    f"Instantiating multiple local models is strictly prohibited.",
                    details={"requested_model": req_model, "resident_model": self.resident_model},
                )

    async def can_admit(self) -> bool:
        async with self._lock:
            return self.active_count < self.max_active

    async def acquire_slot(self) -> bool:
        async with self._lock:
            if self.active_count < self.max_active:
                self.active_count += 1
                return True
            return False

    async def release_slot(self) -> None:
        async with self._lock:
            self.active_count = max(0, self.active_count - 1)


class CloudAdmissionGate:
    """Bounded admission control for cloud inference honoring privacy invariants."""

    def __init__(self, max_active: int = 5) -> None:
        self.max_active = max(1, max_active)
        self.active_count = 0
        self._lock = asyncio.Lock()

    def validate_policy(self, request: SchedulingRequest) -> None:
        """Enforce privacy boundary: only PUBLIC context with explicit cloud enablement can route to cloud."""
        norm_class = (request.data_classification or "INTERNAL").strip().upper()
        if norm_class != "PUBLIC":
            raise ValidationError(
                f"Data classification violation: '{norm_class}' data cannot be routed to cloud inference.",
                field="data_classification",
            )
        if not settings.CLOUD_ENABLED:
            raise ValidationError(
                "Cloud inference is disabled by policy (CLOUD_ENABLED=false).",
                field="cloud_enabled",
            )

    async def can_admit(self) -> bool:
        async with self._lock:
            return self.active_count < self.max_active

    async def acquire_slot(self) -> bool:
        async with self._lock:
            if self.active_count < self.max_active:
                self.active_count += 1
                return True
            return False

    async def release_slot(self) -> None:
        async with self._lock:
            self.active_count = max(0, self.active_count - 1)
