import hashlib
import json
import logging
import math
import re
import uuid
from typing import Any

from pico.core.config import settings
from pico.core.errors import EntityNotFoundError, ValidationError
from pico.homeassistant.client import HomeAssistantClient
from pico.homeassistant.contracts import (
    ClimateObservation,
    ClimateSetpointPlan,
)
from pico.homeassistant.sanitizer import sanitize_entity_attributes
from pico.homeassistant.service import HomeAssistantService

logger = logging.getLogger(__name__)

ENTITY_ID_REGEX = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")

SUPPORTED_UNITS = {
    "°c": "°C",
    "c": "°C",
    "degc": "°C",
    "celsius": "°C",
    "°f": "°F",
    "f": "°F",
    "degf": "°F",
    "fahrenheit": "°F",
    "k": "K",
    "kelvin": "K",
}


def validate_climate_allowlist(raw_allowlist: Any | None = None) -> set[str]:
    """Validate and normalize the Stage D climate entity allowlist.

    Invariants:
    1. Exact string matching only. No wildcards (*, ?), regex, prefixes, or domain wildcards.
    2. Must contain exclusively 'climate.*' domain entity IDs.
    3. Empty or unconfigured allowlist raises ValidationError (fail-closed).
    4. Unnormalized whitespace, empty strings, and non-string entries raise ValidationError.
    5. Returns a set of exact, validated climate entity IDs.
    """
    if raw_allowlist is None:
        raw_allowlist = getattr(settings, "HOME_ASSISTANT_CLIMATE_ALLOWLIST", "[]")

    if isinstance(raw_allowlist, str):
        clean_str = raw_allowlist.strip()
        if not clean_str or clean_str == "[]":
            raise ValidationError("Stage D climate allowlist is empty or unconfigured (HOME_ASSISTANT_CLIMATE_ALLOWLIST).")
        try:
            parsed = json.loads(clean_str)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Malformed JSON in HOME_ASSISTANT_CLIMATE_ALLOWLIST: {exc}")
        if not isinstance(parsed, (list, tuple, set)):
            raise ValidationError("HOME_ASSISTANT_CLIMATE_ALLOWLIST must be a JSON array of entity ID strings.")
        items = list(parsed)
    elif isinstance(raw_allowlist, (list, tuple, set)):
        items = list(raw_allowlist)
    else:
        raise ValidationError("HOME_ASSISTANT_CLIMATE_ALLOWLIST must be a list or JSON array of entity ID strings.")

    if not items:
        raise ValidationError("Stage D climate allowlist is empty. At least one allowlisted climate.* entity is required.")

    validated: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValidationError(f"Allowlist entries must be strings; received {type(item).__name__}: {item!r}")

        if item != item.strip():
            raise ValidationError(f"Whitespace variations in allowlist entry '{item}' are prohibited.")

        clean_item = item.strip()
        if not clean_item:
            raise ValidationError("Empty entity ID string in HOME_ASSISTANT_CLIMATE_ALLOWLIST.")

        if "*" in clean_item or "?" in clean_item or "/" in clean_item or "\\" in clean_item:
            raise ValidationError(f"Wildcards or special characters in entity ID '{clean_item}' are strictly prohibited.")

        if not clean_item.startswith("climate."):
            domain = clean_item.split(".", 1)[0]
            raise ValidationError(
                f"Configured entity '{clean_item}' is in domain '{domain}'. "
                f"Climate awareness is strictly restricted to 'climate.*' entities."
            )

        if not ENTITY_ID_REGEX.match(clean_item):
            raise ValidationError(
                f"Invalid entity ID format '{clean_item}' in HOME_ASSISTANT_CLIMATE_ALLOWLIST. "
                f"Expected format: 'climate.<name>' with alphanumeric/underscore characters only."
            )

        validated.add(clean_item)

    return validated


def parse_temperature(val: Any) -> float | None:
    """Parse and validate numeric temperature metadata. Rejects bools, NaN, and Inf."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f_val = float(val)
        if math.isfinite(f_val):
            return f_val
    return None


def parse_temperature_unit(raw_unit: Any) -> str | None:
    """Parse and canonicalize temperature units into standard representation ('°C', '°F', 'K')."""
    if not isinstance(raw_unit, str):
        return None
    normalized = raw_unit.strip().lower()
    return SUPPORTED_UNITS.get(normalized)


class HomeAssistantClimateService:
    """High-level service for Stage D read-only climate awareness and safe setpoint feasibility.

    Invariants:
    1. STRICTLY READ-ONLY: Never issues POST/PUT/PATCH/DELETE or calls mutation services.
    2. ZERO PHYSICAL MUTATION: Never alters thermostat, HVAC mode, fan, preset, or device state.
    3. FAIL-CLOSED ALLOWLIST: Rejects any entity not present in HOME_ASSISTANT_CLIMATE_ALLOWLIST.
    4. NO FABRICATION: Never invents missing temperatures, units, bounds, or supported modes.
    5. IMMUTABLE DIGEST: Generates reproducible SHA-256 digests for feasibility plans.
    """

    def __init__(
        self,
        service: HomeAssistantService | None = None,
        readonly_client: HomeAssistantClient | None = None,
    ) -> None:
        self._service = service or HomeAssistantService(client=readonly_client)
        self._client = readonly_client or (self._service._client if hasattr(self._service, "_client") else None)

    async def get_climate_observation(self, entity_id: str) -> ClimateObservation:
        """Inspect and return bounded, typed climate observation for an allowlisted entity.

        Guaranteed read-only GET. Never alters state.
        """
        clean_id = entity_id.strip()

        # Check integration and climate awareness enablement
        if not settings.HOME_ASSISTANT_ENABLED:
            return ClimateObservation(
                entity_id=clean_id,
                state="unknown",
                state_readiness="disabled",
                reasons=["Home Assistant integration is disabled by policy (HOME_ASSISTANT_ENABLED=false)."],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        if not getattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False):
            return ClimateObservation(
                entity_id=clean_id,
                state="unknown",
                state_readiness="disabled",
                reasons=["Climate awareness is disabled by policy (HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=false)."],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        # Allowlist check
        try:
            allowlist = validate_climate_allowlist()
        except ValidationError as exc:
            return ClimateObservation(
                entity_id=clean_id,
                state="unknown",
                state_readiness="not_allowlisted",
                reasons=[str(exc)],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        if clean_id not in allowlist:
            return ClimateObservation(
                entity_id=clean_id,
                state="unknown",
                state_readiness="not_allowlisted",
                reasons=[f"Target entity '{clean_id}' is not in HOME_ASSISTANT_CLIMATE_ALLOWLIST."],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        # Authoritative state read (GET only)
        try:
            entity_state = await self._service.get_entity_state(clean_id)
        except EntityNotFoundError:
            return ClimateObservation(
                entity_id=clean_id,
                state="unavailable",
                state_readiness="state_unavailable",
                reasons=[f"Entity '{clean_id}' not found in Home Assistant."],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )
        except Exception as exc:
            return ClimateObservation(
                entity_id=clean_id,
                state="unavailable",
                state_readiness="state_unavailable",
                reasons=[f"Authoritative climate state read failed: {type(exc).__name__}"],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        reported_entity_id = entity_state.entity_id
        if reported_entity_id != clean_id:
            return ClimateObservation(
                entity_id=clean_id,
                state="unknown",
                state_readiness="state_unknown",
                reasons=[f"Response entity ID mismatch: expected '{clean_id}', got '{reported_entity_id}'."],
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        state_str = (entity_state.state or "unknown").strip().lower()
        clean_attrs = sanitize_entity_attributes(entity_state.attributes)

        if state_str in ("unavailable", "unreachable"):
            return ClimateObservation(
                entity_id=clean_id,
                state=state_str,
                state_readiness="state_unavailable",
                reasons=[f"Entity state is reported as '{state_str}'."],
                attributes=clean_attrs,
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        if state_str == "unknown":
            return ClimateObservation(
                entity_id=clean_id,
                state=state_str,
                state_readiness="state_unknown",
                reasons=["Entity state is reported as 'unknown'."],
                attributes=clean_attrs,
                is_mutation_performed=False,
                physical_device_connectivity_claimed=False,
            )

        current_temp = parse_temperature(clean_attrs.get("current_temperature"))
        target_temp = parse_temperature(clean_attrs.get("temperature"))
        min_temp = parse_temperature(clean_attrs.get("min_temp"))
        max_temp = parse_temperature(clean_attrs.get("max_temp"))
        target_temp_step = parse_temperature(clean_attrs.get("target_temp_step"))

        raw_unit = (
            clean_attrs.get("unit_of_measurement")
            or clean_attrs.get("temperature_unit")
            or entity_state.unit_of_measurement
        )
        temp_unit = parse_temperature_unit(raw_unit)

        hvac_modes_raw = clean_attrs.get("hvac_modes")
        hvac_modes = (
            [str(m) for m in hvac_modes_raw if isinstance(m, str)][:10]
            if isinstance(hvac_modes_raw, (list, tuple))
            else []
        )

        reasons: list[str] = []
        state_readiness = "ready"

        # Check unit validity
        if temp_unit is None:
            state_readiness = "unit_unknown"
            reasons.append("Trustworthy temperature unit cannot be established from reported metadata.")

        # Check bounds validity
        if min_temp is None or max_temp is None:
            if state_readiness == "ready":
                state_readiness = "state_unknown"
            reasons.append("Missing minimum or maximum temperature bounds in reported metadata.")
        elif min_temp > max_temp:
            state_readiness = "state_unknown"
            reasons.append(f"Contradictory temperature bounds: min_temp ({min_temp}) > max_temp ({max_temp}).")

        return ClimateObservation(
            entity_id=clean_id,
            domain="climate",
            state=state_str,
            current_temperature=current_temp,
            target_temperature=target_temp,
            min_temp=min_temp,
            max_temp=max_temp,
            target_temp_step=target_temp_step,
            temperature_unit=temp_unit,
            hvac_mode=state_str,
            hvac_modes=hvac_modes,
            state_readiness=state_readiness,
            reasons=reasons,
            data_classification=entity_state.data_classification,
            attributes=clean_attrs,
            is_mutation_performed=False,
            physical_device_connectivity_claimed=False,
        )

    async def assess_setpoint_feasibility(
        self,
        target_entity_id: str,
        requested_temperature: float,
        requested_unit: str | None = None,
    ) -> ClimateSetpointPlan:
        """Assess the feasibility of a requested temperature setpoint without executing mutations.

        A plan is an assessment only. It is not an approval ticket, executable command,
        or authorization to actuate the device.
        """
        clean_id = target_entity_id.strip()
        plan_id = f"plan_climate_{uuid.uuid4().hex[:12]}"

        # Canonical SHA-256 digest
        digest_input = f"{clean_id}:{requested_temperature}:{requested_unit or ''}"
        input_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

        # Reject non-finite or invalid temperature input
        if (
            not isinstance(requested_temperature, (int, float))
            or isinstance(requested_temperature, bool)
            or not math.isfinite(requested_temperature)
        ):
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=float("nan") if not isinstance(requested_temperature, (int, float)) else requested_temperature,
                requested_unit=requested_unit,
                is_feasible=False,
                feasibility_status="state_unknown",
                reasons=["Requested temperature must be a finite numeric value."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Integration & climate awareness enablement checks
        if not settings.HOME_ASSISTANT_ENABLED:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                is_feasible=False,
                feasibility_status="disabled",
                reasons=["Home Assistant integration is disabled by policy (HOME_ASSISTANT_ENABLED=false)."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        if not getattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False):
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                is_feasible=False,
                feasibility_status="disabled",
                reasons=["Climate awareness is disabled by policy (HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED=false)."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Allowlist check
        try:
            allowlist = validate_climate_allowlist()
        except ValidationError as exc:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                is_feasible=False,
                feasibility_status="not_allowlisted",
                reasons=[str(exc)],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        if clean_id not in allowlist:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                is_feasible=False,
                feasibility_status="not_allowlisted",
                reasons=[f"Target entity '{clean_id}' is not in HOME_ASSISTANT_CLIMATE_ALLOWLIST."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Observe entity state (read-only GET)
        obs = await self.get_climate_observation(clean_id)

        # If observation is not ready, inherit the honest uncertainty status
        if obs.state_readiness != "ready":
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=obs.min_temp,
                max_temp=obs.max_temp,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status=obs.state_readiness,
                reasons=obs.reasons or [f"Entity is not ready for planning ({obs.state_readiness})."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # Check unit consistency
        if requested_unit is not None:
            norm_requested_unit = parse_temperature_unit(requested_unit)
            if norm_requested_unit is None:
                return ClimateSetpointPlan(
                    plan_id=plan_id,
                    entity_id=clean_id,
                    requested_temperature=requested_temperature,
                    requested_unit=requested_unit,
                    current_temperature=obs.current_temperature,
                    target_temperature=obs.target_temperature,
                    min_temp=obs.min_temp,
                    max_temp=obs.max_temp,
                    temperature_unit=obs.temperature_unit,
                    hvac_mode=obs.hvac_mode,
                    hvac_modes=obs.hvac_modes,
                    is_feasible=False,
                    feasibility_status="unit_unknown",
                    reasons=[f"Requested temperature unit '{requested_unit}' is unrecognized or unsupported."],
                    is_mutation_performed=False,
                    input_digest=input_digest,
                    data_classification=obs.data_classification,
                )
            if norm_requested_unit != obs.temperature_unit:
                return ClimateSetpointPlan(
                    plan_id=plan_id,
                    entity_id=clean_id,
                    requested_temperature=requested_temperature,
                    requested_unit=requested_unit,
                    current_temperature=obs.current_temperature,
                    target_temperature=obs.target_temperature,
                    min_temp=obs.min_temp,
                    max_temp=obs.max_temp,
                    temperature_unit=obs.temperature_unit,
                    hvac_mode=obs.hvac_mode,
                    hvac_modes=obs.hvac_modes,
                    is_feasible=False,
                    feasibility_status="unit_unknown",
                    reasons=[
                        f"Requested unit '{requested_unit}' conflicts with entity's reported unit '{obs.temperature_unit}'."
                    ],
                    is_mutation_performed=False,
                    input_digest=input_digest,
                    data_classification=obs.data_classification,
                )

        # Validate temperature limits
        min_temp = obs.min_temp
        max_temp = obs.max_temp
        unit_str = obs.temperature_unit or ""

        if min_temp is None or max_temp is None:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="state_unknown",
                reasons=["Missing supported minimum or maximum temperature bounds."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        if requested_temperature < min_temp:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested temperature {requested_temperature}{unit_str} is below minimum supported setpoint {min_temp}{unit_str}."
                ],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        if requested_temperature > max_temp:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested temperature {requested_temperature}{unit_str} is above maximum supported setpoint {max_temp}{unit_str}."
                ],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        return ClimateSetpointPlan(
            plan_id=plan_id,
            entity_id=clean_id,
            requested_temperature=requested_temperature,
            requested_unit=requested_unit,
            current_temperature=obs.current_temperature,
            target_temperature=obs.target_temperature,
            min_temp=min_temp,
            max_temp=max_temp,
            temperature_unit=obs.temperature_unit,
            hvac_mode=obs.hvac_mode,
            hvac_modes=obs.hvac_modes,
            is_feasible=True,
            feasibility_status="ready",
            reasons=[
                f"Requested temperature {requested_temperature}{unit_str} is feasible within reported operating limits [{min_temp}, {max_temp}]."
            ],
            is_mutation_performed=False,
            input_digest=input_digest,
            data_classification=obs.data_classification,
        )
