import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
import httpx

from pico.capabilities.base import VerificationResult
from pico.core.config import settings
from pico.core.errors import AuthorizationError, EntityNotFoundError, ValidationError
from pico.homeassistant.client import (
    ENTITY_ID_REGEX,
    ConnectionError,
    HomeAssistantClient,
)
from pico.homeassistant.contracts import (
    SwitchActuationPlan,
    SwitchActuationResult,
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


def validate_switch_allowlist(raw_allowlist: Any = None) -> set[str]:
    """Strictly validates the Stage C switch allowlist configuration.

    Fails closed on:
    - Missing, None, or empty allowlist.
    - Non-list/set structure (e.g. dict, int, malformed JSON).
    - Wildcards ('*'), prefixes, or regex patterns.
    - Entities outside the 'switch.' domain.
    - Syntactically invalid entity IDs (must match ENTITY_ID_REGEX).
    - Empty, None, or whitespace-untrimmed entries.

    Returns a set of exact, validated switch entity IDs.
    """
    if raw_allowlist is None:
        raw_allowlist = getattr(settings, "HOME_ASSISTANT_SWITCH_ALLOWLIST", "[]")

    if isinstance(raw_allowlist, str):
        clean_str = raw_allowlist.strip()
        if not clean_str or clean_str == "[]":
            raise ValidationError("Stage C switch allowlist is empty or unconfigured (HOME_ASSISTANT_SWITCH_ALLOWLIST).")
        try:
            parsed = json.loads(clean_str)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Malformed JSON in HOME_ASSISTANT_SWITCH_ALLOWLIST: {exc}")
        if not isinstance(parsed, (list, tuple, set)):
            raise ValidationError("HOME_ASSISTANT_SWITCH_ALLOWLIST must be a JSON array of entity ID strings.")
        items = list(parsed)
    elif isinstance(raw_allowlist, (list, tuple, set)):
        items = list(raw_allowlist)
    else:
        raise ValidationError("HOME_ASSISTANT_SWITCH_ALLOWLIST must be a list or JSON array of entity ID strings.")

    if not items:
        raise ValidationError("Stage C switch allowlist is empty. At least one allowlisted switch.* entity is required.")

    validated: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValidationError(f"Allowlist entries must be strings; received {type(item).__name__}: {item!r}")

        # Exact string: reject unnormalized leading/trailing whitespace
        if item != item.strip():
            raise ValidationError(f"Whitespace variations in allowlist entry '{item}' are prohibited.")

        clean_item = item.strip()
        if not clean_item:
            raise ValidationError("Empty entity ID string in HOME_ASSISTANT_SWITCH_ALLOWLIST.")

        # Reject wildcards, prefixes, traversal
        if "*" in clean_item or "?" in clean_item or "/" in clean_item or "\\" in clean_item:
            raise ValidationError(f"Wildcards or special characters in entity ID '{clean_item}' are strictly prohibited.")

        if not clean_item.startswith("switch."):
            domain = clean_item.split(".", 1)[0]
            raise ValidationError(
                f"Configured entity '{clean_item}' is in domain '{domain}'. "
                f"Only the 'switch' domain is permitted for Stage C actuation."
            )

        if not ENTITY_ID_REGEX.match(clean_item):
            raise ValidationError(f"Invalid entity ID format for allowlisted switch: '{clean_item}'.")

        validated.add(clean_item)

    return validated


def validate_switch_actuation_preconditions(
    target_entity_id: str,
    operation: str | None = None,
) -> str:
    """Validate all preconditions required before any Stage C switch actuation may occur.

    Returns the validated target switch entity ID.
    Raises AuthorizationError or ValidationError if any invariant fails.
    """
    # 1. Integration must be enabled
    if not settings.HOME_ASSISTANT_ENABLED:
        raise AuthorizationError("Home Assistant integration is disabled by configuration (HOME_ASSISTANT_ENABLED=false).")

    # 2. Access token must be configured
    if not settings.HOME_ASSISTANT_ACCESS_TOKEN or not settings.HOME_ASSISTANT_ACCESS_TOKEN.strip():
        raise AuthorizationError("Home Assistant access token is missing or empty.")

    # 3. Overall physical actuation must be explicitly enabled
    if not settings.HOME_ASSISTANT_ACTUATION_ENABLED:
        raise AuthorizationError(
            "Physical actuation is disabled by default. Set HOME_ASSISTANT_ACTUATION_ENABLED=true to enable."
        )

    # 4. Stage C switch actuation must be explicitly enabled
    if not getattr(settings, "HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED", False):
        raise AuthorizationError(
            "Stage C switch actuation is disabled by default. Set HOME_ASSISTANT_SWITCH_ACTUATION_ENABLED=true to enable."
        )

    # 5. Allowlist must be valid and non-empty
    allowlist = validate_switch_allowlist()

    # 6. Target entity ID must be provided and exact
    if not target_entity_id or not isinstance(target_entity_id, str):
        raise ValidationError("Target entity ID must be a non-empty string.")

    if target_entity_id != target_entity_id.strip():
        raise ValidationError(f"Target entity ID '{target_entity_id}' contains leading or trailing whitespace.")

    clean_target = target_entity_id.strip()

    if not clean_target.startswith("switch."):
        domain = clean_target.split(".", 1)[0]
        raise ValidationError(
            f"Target entity '{clean_target}' is in domain '{domain}'. "
            f"Only the 'switch' domain is permitted for Stage C actuation."
        )

    if not ENTITY_ID_REGEX.match(clean_target):
        raise ValidationError(f"Invalid entity ID format for target switch: '{clean_target}'.")

    if clean_target not in allowlist:
        raise ValidationError(
            f"Target entity '{clean_target}' is not in the Stage C allowlist ({sorted(list(allowlist))}). "
            f"Actuation of arbitrary, model-chosen, or unconfigured entities is strictly prohibited."
        )

    # 7. If operation or target state provided, validate it
    if operation is not None:
        op = str(operation).strip().lower()
        if op == "toggle":
            raise ValidationError(
                "Operation 'toggle' is prohibited in Stage C due to concurrency ambiguity. "
                "Use explicit 'on' or 'off' (turn_on or turn_off)."
            )
        if op not in ALLOWED_SERVICES and op not in ALLOWED_TARGET_STATES:
            raise ValidationError(
                f"Unsupported operation '{operation}'. Only explicit 'on' (turn_on) and 'off' (turn_off) are permitted."
            )

    return clean_target


class HomeAssistantSwitchActuationService:
    """Dedicated, narrowly scoped service for actuating explicitly allowlisted Stage C Home Assistant switches.

    Enforces:
    1. Only explicitly allowlisted switch.* entities can be targeted.
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
        target_entity_id: str,
        target_state: Literal["on", "off"],
    ) -> SwitchActuationPlan:
        """Create a structured action plan without causing any physical action.

        Inspects the current reported state of the switch via read-only path.
        """
        valid_entity_id = validate_switch_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        desired_state = target_state.strip().lower()
        service = f"turn_{desired_state}"

        try:
            raw_state = await self._readonly_client.get_state(valid_entity_id)
            raw_state_str = str(raw_state.get("state", "")).strip().lower()
            if not raw_state_str or raw_state_str == "unknown":
                current_state = "unknown"
                state_readiness = "state_unknown"
                is_mutation_needed = False
            elif raw_state_str in ("unavailable", "unreachable"):
                current_state = raw_state_str
                state_readiness = "state_unavailable"
                is_mutation_needed = False
            elif raw_state_str not in ("on", "off"):
                current_state = raw_state_str
                state_readiness = "state_unknown"
                is_mutation_needed = False
            elif raw_state_str == desired_state:
                current_state = raw_state_str
                state_readiness = "already_satisfied"
                is_mutation_needed = False
            else:
                current_state = raw_state_str
                state_readiness = "ready"
                is_mutation_needed = True
        except Exception as exc:
            current_state = "unavailable"
            state_readiness = "state_unavailable"
            is_mutation_needed = False
            logger.warning("Could not read current state of switch '%s': %s", valid_entity_id, exc)

        if state_readiness == "ready":
            preconditions = [
                f"Configured switch '{valid_entity_id}' is allowlisted and reachable",
                f"Current state is confirmed '{current_state}' ({state_readiness})",
                "Home Assistant Stage C switch actuation is explicitly enabled",
                "Human operator approval is granted",
            ]
        elif state_readiness == "already_satisfied":
            preconditions = [
                f"Configured switch '{valid_entity_id}' is allowlisted and reachable",
                f"Current state is already confirmed '{desired_state}'",
                "No physical mutation is required",
            ]
        elif state_readiness == "state_unavailable":
            preconditions = [
                f"Configured switch '{valid_entity_id}' is allowlisted",
                "Current state could not be read from Home Assistant (status: state_unavailable)",
                "Physical dispatch will be blocked until state can be read",
            ]
        else:
            preconditions = [
                f"Configured switch '{valid_entity_id}' is allowlisted",
                f"Current reported state is '{current_state}' (status: state_unknown, cannot be interpreted as on/off)",
                "Physical dispatch will be blocked until reliable on/off state is reported",
            ]

        expected_postconditions = [
            f"Reported state of '{valid_entity_id}' equals '{desired_state}'",
            "State verified via independent read-only read-back",
        ]

        canonical_dict = {
            "capability_id": "homeassistant.allowlisted_switch_actuate",
            "entity_id": valid_entity_id,
            "target_state": desired_state,
            "service": service,
        }
        canonical_json = json.dumps(canonical_dict, sort_keys=True, separators=(",", ":"))
        input_digest = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=5)).isoformat()
        plan_id = f"plan_{hashlib.sha256((valid_entity_id + desired_state + now.isoformat()).encode('utf-8')).hexdigest()[:16]}"

        return SwitchActuationPlan(
            plan_id=plan_id,
            entity_id=valid_entity_id,
            requested_state=desired_state,
            current_state=current_state,
            service=service,
            state_readiness=state_readiness,
            is_mutation_needed=is_mutation_needed,
            preconditions=preconditions,
            expected_postconditions=expected_postconditions,
            input_digest=input_digest,
            expires_at=expires_at,
            data_classification="INTERNAL",
        )

    async def dispatch_actuation(
        self,
        target_entity_id: str,
        target_state: Literal["on", "off"],
        execution_id: str | None = None,
        skip_if_already_in_state: bool = True,
    ) -> dict[str, Any]:
        """Dispatch exactly one authenticated mutation request to Home Assistant.

        This is the single, auditable egress point for Stage C physical state changes.
        Retries are strictly prohibited.
        """
        if execution_id:
            if execution_id in self._dispatched_executions:
                raise AuthorizationError(
                    f"Execution '{execution_id}' has already been dispatched. Replay is strictly prohibited."
                )
            self._dispatched_executions.add(execution_id)

        valid_entity_id = validate_switch_actuation_preconditions(
            target_entity_id=target_entity_id,
            operation=target_state,
        )

        desired_state = target_state.strip().lower()
        service = f"turn_{desired_state}"

        # Pre-dispatch authoritative state inspection
        try:
            raw_state = await self._readonly_client.get_state(valid_entity_id)
            reported_raw = str(raw_state.get("state", "")).strip().lower()
        except EntityNotFoundError as exc:
            logger.warning("Pre-dispatch check: entity '%s' not found: %s", valid_entity_id, exc)
            return {
                "dispatched": False,
                "service": service,
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "blocked_precondition",
                "reason": f"Target entity '{valid_entity_id}' not found in Home Assistant (HTTP 404). Precondition failed.",
                "reported_state": "unavailable",
            }
        except Exception as exc:
            logger.warning("Pre-dispatch check failed for switch '%s': %s", valid_entity_id, exc)
            return {
                "dispatched": False,
                "service": service,
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "blocked_precondition",
                "reason": f"Pre-dispatch state read failed for '{valid_entity_id}' ({type(exc).__name__}). Precondition could not be verified.",
                "reported_state": "unavailable",
            }

        # Validate reported state: must be reliably interpretable as on or off
        if not reported_raw or reported_raw in ("unknown", "unavailable", "unreachable") or reported_raw not in ("on", "off"):
            logger.warning(
                "Pre-dispatch check: switch '%s' reported state '%s', which cannot be safely interpreted as on/off.",
                valid_entity_id,
                reported_raw,
            )
            return {
                "dispatched": False,
                "service": service,
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "blocked_precondition",
                "reason": (
                    f"Target entity '{valid_entity_id}' reported state '{reported_raw or 'empty'}'. "
                    f"Physical mutation blocked because reliable pre-actuation state could not be established."
                ),
                "reported_state": reported_raw or "unknown",
            }

        # If already in requested state, avoid unnecessary physical mutation
        if skip_if_already_in_state and reported_raw == desired_state:
            logger.info(
                "Pre-dispatch check: switch '%s' is already in desired state '%s'. Mutation skipped.",
                valid_entity_id,
                desired_state,
            )
            return {
                "dispatched": False,
                "service": service,
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "already_satisfied",
                "reason": f"Target switch '{valid_entity_id}' is already in requested state '{desired_state}'.",
                "reported_state": desired_state,
            }

        url = f"{self._base_url}/api/services/switch/{service}"
        headers = self._get_headers()
        payload = {"entity_id": valid_entity_id}

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
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": resp.status_code,
                "dispatch_status": "accepted",
                "reported_state": reported_raw,
            }
        except httpx.TimeoutException:
            logger.warning("Actuation dispatch timed out after %ss for %s", self._timeout, valid_entity_id)
            return {
                "dispatched": True,
                "service": service,
                "entity_id": valid_entity_id,
                "target_state": desired_state,
                "http_status": None,
                "dispatch_status": "timeout_uncertain",
                "reported_state": reported_raw,
            }
        except httpx.RequestError as exc:
            logger.error("Actuation dispatch failed: %s", type(exc).__name__)
            raise ConnectionError(f"Actuation dispatch failed: {type(exc).__name__}")

    async def verify_state(
        self,
        target_entity_id: str,
        expected_state: Literal["on", "off"],
        max_timeout: float | None = None,
        poll_interval: float | None = None,
    ) -> VerificationResult:
        """Independently verify reported entity state using read-only path.

        Bounded polling: stops when state matches or timeout expires.
        Zero state-changing requests are made.
        """
        valid_entity_id = validate_switch_actuation_preconditions(
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
                raw = await self._readonly_client.get_state(valid_entity_id)
                last_state = str(raw.get("state", "unknown")).lower()
                if last_state == desired_state:
                    return VerificationResult(
                        passed=True,
                        details={
                            "entity_id": valid_entity_id,
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
                f"State verification failed for switch '{valid_entity_id}': "
                f"expected state '{desired_state}', but reported state remained '{last_state}' after {attempts} attempts ({timeout}s)."
            ),
            details={
                "entity_id": valid_entity_id,
                "expected_state": desired_state,
                "reported_state": last_state,
                "attempts": attempts,
                "physical_device_connectivity_claimed": False,
            },
        )
