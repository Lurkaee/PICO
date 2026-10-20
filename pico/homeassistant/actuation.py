import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
import httpx

from pico.capabilities.base import VerificationResult
from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.homeassistant.client import (
    ENTITY_ID_REGEX,
    ConnectionError,
    HomeAssistantClient,
)
from pico.homeassistant.contracts import (
    TestLightActuationPlan,
    TestLightActuationResult,
)

logger = logging.getLogger(__name__)

ALLOWED_SERVICES = {"turn_on", "turn_off"}
ALLOWED_TARGET_STATES = {"on", "off"}


def validate_actuation_preconditions(
    target_entity_id: str | None = None,
    operation: str | None = None,
) -> str:
    """Validate all preconditions required before any physical actuation may occur.

    Returns the validated test light entity ID.
    Raises AuthorizationError or ValidationError if any invariant fails.
    """
    # 1. Integration must be enabled
    if not settings.HOME_ASSISTANT_ENABLED:
        raise AuthorizationError("Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).")

    # 2. Access token must be configured
    if not settings.HOME_ASSISTANT_ACCESS_TOKEN or not settings.HOME_ASSISTANT_ACCESS_TOKEN.strip():
        raise AuthorizationError("Home Assistant access token is missing or empty.")

    # 3. Physical actuation must be explicitly enabled
    if not settings.HOME_ASSISTANT_ACTUATION_ENABLED:
        raise AuthorizationError(
            "Physical actuation is disabled by default. Set HOME_ASSISTANT_ACTUATION_ENABLED=true to enable."
        )

    # 4. Configured test light entity ID must be present and valid
    configured_light = settings.HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID
    if not configured_light or not configured_light.strip():
        raise ValidationError(
            "Home Assistant P7 test light entity ID is not configured (HOME_ASSISTANT_P7_TEST_LIGHT_ENTITY_ID). "
            "Actuation is refused without a designated target."
        )

    configured_light = configured_light.strip()
    if not ENTITY_ID_REGEX.match(configured_light):
        raise ValidationError(f"Invalid entity ID format for test light: '{configured_light}'.")

    if not configured_light.startswith("light."):
        domain = configured_light.split(".", 1)[0]
        raise ValidationError(
            f"Configured test light entity '{configured_light}' is in domain '{domain}'. "
            f"Only the 'light' domain is permitted for physical actuation in Milestone P7.3."
        )

    # 5. If caller provided target_entity_id, it must match configured test light exactly
    if target_entity_id is not None:
        clean_target = target_entity_id.strip()
        if clean_target != configured_light:
            raise ValidationError(
                f"Target entity '{clean_target}' does not match configured test light '{configured_light}'. "
                f"Actuation of arbitrary, model-chosen, or unconfigured entities is strictly prohibited."
            )

    # 6. If operation or target state provided, validate it
    if operation is not None:
        op = operation.strip().lower()
        if op == "toggle":
            raise ValidationError(
                "Operation 'toggle' is prohibited in Milestone P7.3 due to concurrency ambiguity. "
                "Use explicit 'on' or 'off' (turn_on or turn_off)."
            )
        if op not in ALLOWED_SERVICES and op not in ALLOWED_TARGET_STATES:
            raise ValidationError(
                f"Unsupported operation '{operation}'. Only explicit 'on' (turn_on) and 'off' (turn_off) are permitted."
            )

    return configured_light


class HomeAssistantActuationService:
    """Dedicated, narrowly scoped service for actuating the single configured Home Assistant test light.

    Enforces:
    1. Only the single configured test light can be targeted.
    2. Only explicit 'turn_on' and 'turn_off' operations are permitted.
    3. Stage A read-only WebSocket boundary is preserved (never used for writes).
    4. Outbound dispatch is through authenticated REST API with strict timeouts and no retries.
    5. Independent read-after-write verification confirms reported state.
    """

    def __init__(
        self,
        readonly_client: HomeAssistantClient | None = None,
        http_client: httpx.AsyncClient | None = None,
        base_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        self._readonly_client = readonly_client or HomeAssistantClient(
            base_url=base_url,
            access_token=access_token,
        )
        self._http_client = http_client
        self._base_url = (base_url or settings.HOME_ASSISTANT_BASE_URL).strip().rstrip("/")
        self._access_token = access_token if access_token is not None else settings.HOME_ASSISTANT_ACCESS_TOKEN
        self._timeout = settings.HOME_ASSISTANT_ACTUATION_TIMEOUT_SECONDS
        self._dispatched_executions: set[str] = set()

    def _get_headers(self) -> dict[str, str]:
        if not self._access_token or not self._access_token.strip():
            raise AuthorizationError("Missing Home Assistant access token for actuation.")
        return {
            "Authorization": f"Bearer {self._access_token.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client:
            return self._http_client
        return httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)

    async def create_plan(
        self,
        target_state: Literal["on", "off"],
        target_entity_id: str | None = None,
    ) -> TestLightActuationPlan:
        """Create a structured action plan without causing any physical action.

        Inspects the current reported state of the configured test light via read-only path.
        """
        test_light_id = validate_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        # Read current state through established read-only client
        try:
            raw_state = await self._readonly_client.get_state(test_light_id)
            current_state = str(raw_state.get("state", "unknown")).lower()
        except Exception as exc:
            current_state = "unreachable"
            logger.warning("Could not read current state of test light: %s", exc)

        desired_state = target_state.strip().lower()
        service = f"turn_{desired_state}"
        is_mutation_needed = (current_state != desired_state) and (current_state != "unreachable")

        preconditions = [
            f"Configured test light '{test_light_id}' is reachable",
            f"Current state is '{current_state}'",
            "Physical actuation is explicitly enabled",
            "Human operator approval is granted",
        ]
        expected_postconditions = [
            f"Reported state of '{test_light_id}' equals '{desired_state}'",
            "State verified via independent read-only read-back",
        ]

        # Deterministic canonical payload for digest calculation
        canonical_dict = {
            "capability_id": "homeassistant.test_light_actuate",
            "entity_id": test_light_id,
            "target_state": desired_state,
            "service": service,
        }
        canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
        input_digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=5)).isoformat()
        plan_id = f"plan_{hashlib.sha256((test_light_id + desired_state + now.isoformat()).encode('utf-8')).hexdigest()[:16]}"

        return TestLightActuationPlan(
            plan_id=plan_id,
            entity_id=test_light_id,
            requested_state=desired_state,
            current_state=current_state,
            service=service,
            is_mutation_needed=is_mutation_needed,
            preconditions=preconditions,
            expected_postconditions=expected_postconditions,
            input_digest=input_digest,
            expires_at=expires_at,
            data_classification="INTERNAL",
        )

    async def dispatch_actuation(
        self,
        target_state: Literal["on", "off"],
        target_entity_id: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch exactly one authenticated mutation request to Home Assistant.

        This is the single, auditable egress point for physical state changes.
        Retries are strictly prohibited.
        """
        if execution_id:
            if execution_id in self._dispatched_executions:
                raise AuthorizationError(
                    f"Execution '{execution_id}' has already been dispatched. Replay is strictly prohibited."
                )
            self._dispatched_executions.add(execution_id)

        test_light_id = validate_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        desired_state = target_state.strip().lower()
        service = f"turn_{desired_state}"
        url = f"{self._base_url}/api/services/light/{service}"
        headers = self._get_headers()
        payload = {"entity_id": test_light_id}

        client = await self._get_http_client()
        try:
            resp = await client.post(url, headers=headers, json=payload, timeout=self._timeout)
            if resp.status_code == 401 or resp.status_code == 403:
                raise AuthorizationError("Home Assistant rejected actuation credentials (401/403).")
            if resp.status_code >= 400:
                raise ConnectionError(f"Home Assistant returned HTTP error on service dispatch: {resp.status_code}")

            return {
                "dispatched": True,
                "service": service,
                "entity_id": test_light_id,
                "target_state": desired_state,
                "http_status": resp.status_code,
                "dispatch_status": "accepted",
            }
        except httpx.TimeoutException:
            # Timeout during dispatch: mutation may or may not have happened.
            # Do NOT retry. Return uncertain dispatch status to be resolved via verification.
            logger.warning("Actuation dispatch timed out after %ss for %s", self._timeout, test_light_id)
            return {
                "dispatched": True,
                "service": service,
                "entity_id": test_light_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "timeout_uncertain",
            }
        except httpx.RequestError as exc:
            logger.error("Actuation dispatch failed: %s", type(exc).__name__)
            raise ConnectionError(f"Actuation dispatch failed: {type(exc).__name__}")

    async def verify_state(
        self,
        expected_state: Literal["on", "off"],
        target_entity_id: str | None = None,
        max_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> VerificationResult:
        """Independently verify reported entity state using read-only path.

        Bounded polling: stops when state matches or timeout expires.
        Zero state-changing requests are made.
        """
        test_light_id = validate_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=expected_state,
        )
        desired_state = expected_state.strip().lower()
        timeout = max_timeout if max_timeout is not None else settings.HOME_ASSISTANT_VERIFICATION_TIMEOUT_SECONDS
        interval = poll_interval if poll_interval is not None else settings.HOME_ASSISTANT_VERIFICATION_POLL_INTERVAL_SECONDS

        start_time = asyncio.get_event_loop().time()
        last_state = "unknown"
        attempts = 0

        while True:
            attempts += 1
            try:
                raw = await self._readonly_client.get_state(test_light_id)
                last_state = str(raw.get("state", "unknown")).lower()
                if last_state == desired_state:
                    return VerificationResult(
                        passed=True,
                        details={
                            "entity_id": test_light_id,
                            "expected_state": desired_state,
                            "reported_state": last_state,
                            "attempts": attempts,
                            "physical_device_connectivity_claimed": False,
                        },
                    )
            except Exception as exc:
                last_state = f"read_error: {type(exc).__name__}"

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                break

            await asyncio.sleep(min(interval, max(0.05, timeout - elapsed)))

        return VerificationResult(
            passed=False,
            reason=(
                f"State verification failed for '{test_light_id}': "
                f"expected state '{desired_state}', but reported state remained '{last_state}' after {attempts} attempts ({timeout}s)."
            ),
            details={
                "entity_id": test_light_id,
                "expected_state": desired_state,
                "reported_state": last_state,
                "attempts": attempts,
                "physical_device_connectivity_claimed": False,
            },
        )
