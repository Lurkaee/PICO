from decimal import Decimal, InvalidOperation
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
    ClimateDualSetpointPlan,
    ClimateObservation,
    ClimateSetpointPlan,
)
from pico.homeassistant.sanitizer import sanitize_entity_attributes
from pico.homeassistant.service import HomeAssistantService

logger = logging.getLogger(__name__)

ENTITY_ID_REGEX = re.compile(r"^[a-zA-Z0-9_]+\.[a-zA-Z0-9_]+$")

# Home Assistant ClimateEntityFeature bit constants (verified from homeassistant.components.climate.const)
HA_CLIMATE_FEATURE_TARGET_TEMPERATURE = 1
HA_CLIMATE_FEATURE_TARGET_TEMPERATURE_RANGE = 2

# Operating modes compatible with a single target temperature setpoint request.
# Home Assistant ClimateEntity contract:
# - 'heat': device heats to a single target temperature.
# - 'cool': device cools to a single target temperature.
SINGLE_SETPOINT_COMPATIBLE_MODES = {"heat", "cool"}

# Documented mode incompatibility reasons for single-setpoint planning
MODE_INCOMPATIBILITY_REASONS = {
    "off": "Entity is reported in 'off' mode and cannot accept temperature setpoints while powered off.",
    "fan_only": "Entity is reported in 'fan_only' mode which only runs the fan without temperature regulation.",
    "auto": "Entity is reported in 'auto' mode; Home Assistant documents that a target temperature cannot be set in auto mode.",
    "heat_cool": "Entity is reported in 'heat_cool' mode which requires a dual-setpoint temperature range (target_temp_low/target_temp_high); single-setpoint requests are incompatible with heat_cool mode.",
    "dry": "Entity is reported in 'dry' mode; the Home Assistant climate contract does not reliably establish temperature setpoint compatibility for dry mode.",
}

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


def parse_temperature_step(val: Any) -> float | None:
    """Parse and validate temperature step metadata.

    Must be a strictly positive, finite numeric value. Rejects bools, <=0, NaN, Inf.
    """
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        f_val = float(val)
        if math.isfinite(f_val) and f_val > 0:
            return f_val
    return None


def parse_supported_features(val: Any) -> int | None:
    """Parse and validate supported_features bitmask. Rejects bools, floats, negatives, and non-ints."""
    if val is None or isinstance(val, bool) or not isinstance(val, int):
        return None
    if 0 <= val <= 1048575:
        return val
    return None


def validate_temperature_step(
    requested_temperature: float,
    step: float | None,
    raw_step: Any = None,
    grid_origin: float | None = None,
) -> tuple[bool, str, str | None]:
    """Validate requested temperature against reported target_temp_step.

    Invariants:
    1. Returns (False, "step_unknown", reason) if step is missing, zero, negative, boolean, or invalid.
    2. Uses Decimal for exact decimal increment testing; rejects floating-point imprecision.
    3. If grid_origin is not established (None), returns (False, "step_alignment_unknown", reason)
       because the upstream Home Assistant ClimateEntity contract specifies the increment granularity
       without establishing a step-grid origin (neither zero nor min_temp is an authoritative grid anchor).
    4. If grid_origin is established, checks exact alignment on the grid: (abs(requested - grid_origin) % step == 0).
       Returns (False, "target_step_mismatch", reason) if the requested setpoint conflicts with the step.
    5. Returns (True, "ready", None) when the setpoint strictly aligns with the verified grid.
    """
    if raw_step is None and step is None:
        return (
            False,
            "step_unknown",
            "Entity does not report target_temp_step metadata; step compatibility cannot be reliably established.",
        )
    if step is None or step <= 0 or not math.isfinite(step) or isinstance(raw_step, bool):
        display_step = raw_step if raw_step is not None else step
        return (
            False,
            "step_unknown",
            f"Entity reported invalid target_temp_step ({display_step!r}); step metadata must be a positive, finite number.",
        )

    try:
        dec_req = Decimal(str(requested_temperature))
        dec_step = Decimal(str(step))
    except (InvalidOperation, ValueError):
        return (
            False,
            "step_unknown",
            f"Unable to parse requested temperature ({requested_temperature}) or step ({step}) as decimal.",
        )

    if grid_origin is None:
        return (
            False,
            "step_alignment_unknown",
            f"Entity reports target_temp_step={step}, but the upstream Home Assistant contract does not establish "
            "a step-grid origin (neither zero nor min_temp is an authoritative grid anchor); "
            "step alignment cannot be reliably proven from reported metadata.",
        )

    if (
        not isinstance(grid_origin, (int, float))
        or isinstance(grid_origin, bool)
        or not math.isfinite(float(grid_origin))
        or float(grid_origin) < 0
    ):
        return (
            False,
            "step_alignment_unknown",
            f"Invalid grid_origin ({grid_origin!r}); must be a non-negative finite numeric value.",
        )

    try:
        dec_origin = Decimal(str(grid_origin))
    except (InvalidOperation, ValueError):
        return (
            False,
            "step_alignment_unknown",
            f"Unable to parse grid_origin ({grid_origin}) as decimal.",
        )

    offset = abs(dec_req - dec_origin)
    remainder = offset % dec_step
    tolerance = Decimal("1e-6")
    if remainder > tolerance and (dec_step - remainder) > tolerance:
        return (
            False,
            "target_step_mismatch",
            f"Requested temperature {requested_temperature} does not align with supported step increment {step} "
            f"from established grid origin {grid_origin}.",
        )

    return (True, "ready", None)


def validate_temperature_capability(
    supported_features: int | None,
    raw_supported_features: Any = None,
) -> tuple[bool, str, str | None]:
    """Validate whether the climate entity supports single target temperature setpoints.

    Verified Home Assistant contract:
    - TARGET_TEMPERATURE (bit 0, value 1): single setpoint support.
    - TARGET_TEMPERATURE_RANGE (bit 1, value 2): dual low/high setpoint range support.

    Returns (is_supported, status, reason).
    """
    if raw_supported_features is None and supported_features is None:
        return (
            False,
            "capability_unknown",
            "Entity does not report trustworthy supported_features capability metadata; setpoint capability cannot be reliably established.",
        )
    if supported_features is None or supported_features < 0:
        display_feat = raw_supported_features if raw_supported_features is not None else supported_features
        return (
            False,
            "capability_unknown",
            f"Entity reported invalid supported_features capability metadata ({display_feat!r}).",
        )

    # Check for single target temperature support (Bit 0, value 1)
    if (supported_features & HA_CLIMATE_FEATURE_TARGET_TEMPERATURE) != 0:
        return (True, "ready", None)

    # If Bit 0 is not set, check if it only supports target temperature range (Bit 1, value 2)
    if (supported_features & HA_CLIMATE_FEATURE_TARGET_TEMPERATURE_RANGE) != 0:
        return (
            False,
            "unsupported_capability",
            f"Entity does not support single target temperature setpoints (supported_features={supported_features}); entity only advertises dual-setpoint temperature range capability.",
        )

    return (
        False,
        "unsupported_capability",
        f"Entity does not support target temperature operations (supported_features={supported_features}).",
    )


def validate_mode_compatibility(
    hvac_mode: str | None,
) -> tuple[bool, str, str | None]:
    """Check if reported HVAC mode permits setting a single target temperature.

    Home Assistant ClimateEntity contract:
    - 'off': incompatible with setting a temperature.
    - 'fan_only': only fan runs, no temperature regulation.
    - 'auto': HA documentation explicitly specifies temperature cannot be set in auto mode.
    - 'heat_cool': uses dual target temperature range (low/high), incompatible with single setpoint.
    - 'dry': primarily dehumidification; contract does not reliably establish temperature setpoint compatibility.
    - 'heat' and 'cool': compatible with single-setpoint requests if capability is supported.

    Returns (is_compatible, status, reason).
    """
    if not hvac_mode:
        return (
            False,
            "state_unknown",
            "Entity reports empty or missing operating mode.",
        )

    mode_lower = hvac_mode.strip().lower()

    if mode_lower in MODE_INCOMPATIBILITY_REASONS:
        return (
            False,
            "mode_incompatible",
            MODE_INCOMPATIBILITY_REASONS[mode_lower],
        )

    if mode_lower not in SINGLE_SETPOINT_COMPATIBLE_MODES:
        return (
            False,
            "mode_incompatible",
            f"Reported operating mode '{hvac_mode}' is not compatible with setting a single target temperature setpoint.",
        )

    return (True, "ready", None)


def validate_dual_temperature_capability(
    supported_features: int | None,
    raw_supported_features: Any = None,
) -> tuple[bool, str, str | None]:
    """Validate whether the climate entity supports dual target temperature setpoints.

    Verified Home Assistant contract:
    - TARGET_TEMPERATURE (bit 0, value 1): single setpoint support.
    - TARGET_TEMPERATURE_RANGE (bit 1, value 2): dual low/high setpoint range support.

    Returns (is_supported, status, reason).
    """
    if raw_supported_features is None and supported_features is None:
        return (
            False,
            "capability_unknown",
            "Entity does not report trustworthy supported_features capability metadata; setpoint capability cannot be reliably established.",
        )
    if supported_features is None or supported_features < 0:
        display_feat = raw_supported_features if raw_supported_features is not None else supported_features
        return (
            False,
            "capability_unknown",
            f"Entity reported invalid supported_features capability metadata ({display_feat!r}).",
        )

    # Check for dual target temperature range support (Bit 1, value 2)
    if (supported_features & HA_CLIMATE_FEATURE_TARGET_TEMPERATURE_RANGE) != 0:
        return (True, "ready", None)

    # If Bit 1 is not set, check if it only supports single setpoint (Bit 0, value 1)
    if (supported_features & HA_CLIMATE_FEATURE_TARGET_TEMPERATURE) != 0:
        return (
            False,
            "unsupported_capability",
            f"Entity does not support dual-setpoint temperature range (supported_features={supported_features}); entity only advertises single-setpoint target temperature capability.",
        )

    return (
        False,
        "unsupported_capability",
        f"Entity does not support target temperature range operations (supported_features={supported_features}).",
    )


def validate_dual_mode_compatibility(
    hvac_mode: str | None,
) -> tuple[bool, str, str | None]:
    """Check if reported HVAC mode permits dual target temperature range planning.

    Home Assistant ClimateEntity contract:
    - Only 'heat_cool' mode uses a dual target temperature range (target_temp_low / target_temp_high).
    - 'heat' and 'cool' only support a single target temperature setpoint.
    - 'off', 'fan_only', 'auto', 'dry' do not permit dual-setpoint planning.

    Returns (is_compatible, status, reason).
    """
    if not hvac_mode:
        return (
            False,
            "state_unknown",
            "Entity reports empty or missing operating mode.",
        )

    mode_lower = hvac_mode.strip().lower()

    if mode_lower == "heat_cool":
        return (True, "ready", None)

    if mode_lower in ("heat", "cool"):
        return (
            False,
            "mode_incompatible",
            f"Entity is reported in '{mode_lower}' mode which only supports a single target temperature setpoint; "
            "dual-setpoint range requests require 'heat_cool' mode.",
        )

    if mode_lower in MODE_INCOMPATIBILITY_REASONS:
        return (
            False,
            "mode_incompatible",
            f"{MODE_INCOMPATIBILITY_REASONS[mode_lower]} Dual-setpoint range requests require 'heat_cool' mode.",
        )

    return (
        False,
        "mode_incompatible",
        f"Reported operating mode '{hvac_mode}' is not compatible with dual-setpoint temperature range requests; 'heat_cool' mode is required.",
    )



def parse_temperature_unit(raw_unit: Any) -> str | None:
    """Parse and canonicalize temperature units into standard representation ('°C', '°F', 'K')."""
    if not isinstance(raw_unit, str):
        return None
    normalized = raw_unit.strip().lower()
    return SUPPORTED_UNITS.get(normalized)


def validate_climate_grid_origins(
    raw_config: Any | None = None,
    allowlist: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate and normalize operator-controlled per-entity climate step-grid origins.

    Invariants:
    1. Exact string matching only. No wildcards, regex, prefixes, or domain wildcards.
    2. Must map exact 'climate.*' entity IDs to objects with exactly {'origin', 'unit'}.
    3. Entities must be present in the Stage D climate allowlist.
    4. Origin must be a finite, non-negative, non-boolean numeric value.
    5. Unit must be a recognized, supported temperature unit ('°C', '°F', 'K').
    6. Malformed JSON, duplicate keys, extra/unexpected fields, or invalid types fail closed with ValidationError.
    7. Empty configuration ('{}') is valid and denotes no entity has an established grid origin.
    """
    if raw_config is None:
        raw_config = getattr(settings, "HOME_ASSISTANT_CLIMATE_GRID_ORIGINS", "{}")

    if isinstance(raw_config, str):
        clean_str = raw_config.strip()
        if not clean_str or clean_str == "{}":
            return {}

        def check_duplicate_keys(pairs: list[tuple[Any, Any]]) -> dict[Any, Any]:
            res: dict[Any, Any] = {}
            for k, v in pairs:
                if k in res:
                    raise ValidationError(f"Duplicate entity ID '{k}' in HOME_ASSISTANT_CLIMATE_GRID_ORIGINS.")
                res[k] = v
            return res

        try:
            parsed = json.loads(clean_str, object_pairs_hook=check_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Malformed JSON in HOME_ASSISTANT_CLIMATE_GRID_ORIGINS: {exc}")
    elif isinstance(raw_config, dict):
        parsed = raw_config
    else:
        raise ValidationError("HOME_ASSISTANT_CLIMATE_GRID_ORIGINS must be a JSON object or dictionary.")

    if not isinstance(parsed, dict):
        raise ValidationError("HOME_ASSISTANT_CLIMATE_GRID_ORIGINS must be a JSON object or dictionary mapping entity IDs to configuration objects.")

    if not parsed:
        return {}

    if allowlist is None:
        allowlist = validate_climate_allowlist()

    validated: dict[str, dict[str, Any]] = {}
    for entity_id, entry in parsed.items():
        if not isinstance(entity_id, str):
            raise ValidationError(f"Grid origin entity ID keys must be strings; received {type(entity_id).__name__}: {entity_id!r}")

        if entity_id != entity_id.strip():
            raise ValidationError(f"Whitespace variations in grid origin entity ID '{entity_id}' are prohibited.")

        clean_id = entity_id.strip()
        if not clean_id:
            raise ValidationError("Empty entity ID string in HOME_ASSISTANT_CLIMATE_GRID_ORIGINS.")

        if any(ch in clean_id for ch in ("*", "?", "/", "\\")):
            raise ValidationError(f"Wildcards or special characters in grid origin entity ID '{clean_id}' are prohibited.")

        if not clean_id.startswith("climate."):
            domain = clean_id.split(".", 1)[0]
            raise ValidationError(
                f"Configured entity '{clean_id}' is in domain '{domain}'. "
                "Climate grid origins are strictly restricted to 'climate.*' entities."
            )

        if not ENTITY_ID_REGEX.match(clean_id):
            raise ValidationError(
                f"Invalid entity ID format '{clean_id}' in HOME_ASSISTANT_CLIMATE_GRID_ORIGINS. "
                "Expected format: 'climate.<name>' with alphanumeric/underscore characters only."
            )

        if clean_id not in allowlist:
            raise ValidationError(
                f"Configured climate grid origin entity '{clean_id}' is not in HOME_ASSISTANT_CLIMATE_ALLOWLIST."
            )

        if not isinstance(entry, dict):
            raise ValidationError(
                f"Grid origin configuration for '{clean_id}' must be an object/dict; received {type(entry).__name__}."
            )

        if set(entry.keys()) != {"origin", "unit"}:
            unexpected = set(entry.keys()) - {"origin", "unit"}
            missing = {"origin", "unit"} - set(entry.keys())
            detail = f"unexpected fields: {unexpected}" if unexpected else f"missing fields: {missing}"
            raise ValidationError(
                f"Grid origin configuration for '{clean_id}' must contain exactly 'origin' and 'unit' fields ({detail})."
            )

        raw_origin = entry["origin"]
        if raw_origin is None or isinstance(raw_origin, bool) or not isinstance(raw_origin, (int, float)):
            raise ValidationError(
                f"Invalid origin {raw_origin!r} for '{clean_id}'; must be a finite numeric value."
            )
        f_origin = float(raw_origin)
        if not math.isfinite(f_origin):
            raise ValidationError(
                f"Non-finite origin {raw_origin!r} for '{clean_id}' is not permitted."
            )
        if f_origin < 0:
            raise ValidationError(
                f"Invalid origin {raw_origin!r} for '{clean_id}'; negative origins are not permitted."
            )

        raw_unit = entry["unit"]
        if not isinstance(raw_unit, str):
            raise ValidationError(
                f"Unit in grid origin configuration for '{clean_id}' must be a string; received {type(raw_unit).__name__}."
            )
        norm_unit = parse_temperature_unit(raw_unit)
        if norm_unit is None:
            raise ValidationError(
                f"Unrecognized temperature unit '{raw_unit}' in grid origin configuration for '{clean_id}'."
            )

        validated[clean_id] = {
            "origin": f_origin,
            "unit": norm_unit,
        }

    return validated


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
        trusted_grid_origins: Any = None,
    ) -> None:
        self._service = service or HomeAssistantService(client=readonly_client)
        self._client = readonly_client or (self._service._client if hasattr(self._service, "_client") else None)
        self._trusted_grid_origins_raw = trusted_grid_origins

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
        target_temp_low = parse_temperature(clean_attrs.get("target_temp_low"))
        target_temp_high = parse_temperature(clean_attrs.get("target_temp_high"))
        min_temp = parse_temperature(clean_attrs.get("min_temp"))
        max_temp = parse_temperature(clean_attrs.get("max_temp"))
        target_temp_step = parse_temperature_step(clean_attrs.get("target_temp_step"))
        supported_features = parse_supported_features(clean_attrs.get("supported_features"))

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
            target_temp_low=target_temp_low,
            target_temp_high=target_temp_high,
            min_temp=min_temp,
            max_temp=max_temp,
            target_temp_step=target_temp_step,
            supported_features=supported_features,
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

        # Resolve effective server-configured grid origin for this entity
        effective_origin: float | None = None
        effective_origin_unit: str | None = None
        grid_config_error: str | None = None

        try:
            current_allowlist = validate_climate_allowlist()
            origins_map = validate_climate_grid_origins(
                raw_config=self._trusted_grid_origins_raw,
                allowlist=current_allowlist,
            )
            if clean_id in origins_map:
                effective_origin = origins_map[clean_id]["origin"]
                effective_origin_unit = origins_map[clean_id]["unit"]
        except ValidationError as exc:
            grid_config_error = str(exc)
        except Exception as exc:
            grid_config_error = f"Failed to load trusted grid origin configuration: {exc}"

        # Canonical SHA-256 digest binding exact entity ID, requested temperature, requested unit, and effective origin + unit
        origin_digest_str = f"{effective_origin}:{effective_origin_unit}" if effective_origin is not None else "none:none"
        digest_input = f"{clean_id}:{requested_temperature}:{requested_unit or ''}:{origin_digest_str}"
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=obs.min_temp,
                max_temp=obs.max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
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

        # 1. Capability check (supported_features)
        raw_feat = obs.attributes.get("supported_features")
        is_cap_ok, cap_status, cap_reason = validate_temperature_capability(
            supported_features=obs.supported_features,
            raw_supported_features=raw_feat,
        )
        if not is_cap_ok:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=obs.min_temp,
                max_temp=obs.max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status=cap_status,
                reasons=[cap_reason] if cap_reason else ["Capability validation failed."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # 2. Operating mode compatibility check (hvac_mode)
        is_mode_ok, mode_status, mode_reason = validate_mode_compatibility(obs.hvac_mode)
        if not is_mode_ok:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=obs.min_temp,
                max_temp=obs.max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status=mode_status,
                reasons=[mode_reason] if mode_reason else ["Operating mode is incompatible with temperature setpoints."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # 3. Check unit consistency
        if requested_unit is not None:
            norm_requested_unit = parse_temperature_unit(requested_unit)
            if norm_requested_unit is None:
                return ClimateSetpointPlan(
                    plan_id=plan_id,
                    entity_id=clean_id,
                    requested_temperature=requested_temperature,
                    requested_unit=requested_unit,
                    grid_origin=effective_origin,
                    grid_origin_unit=effective_origin_unit,
                    current_temperature=obs.current_temperature,
                    target_temperature=obs.target_temperature,
                    min_temp=obs.min_temp,
                    max_temp=obs.max_temp,
                    target_temp_step=obs.target_temp_step,
                    supported_features=obs.supported_features,
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
                    grid_origin=effective_origin,
                    grid_origin_unit=effective_origin_unit,
                    current_temperature=obs.current_temperature,
                    target_temperature=obs.target_temperature,
                    min_temp=obs.min_temp,
                    max_temp=obs.max_temp,
                    target_temp_step=obs.target_temp_step,
                    supported_features=obs.supported_features,
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

        # 4. Validate temperature limits
        min_temp = obs.min_temp
        max_temp = obs.max_temp
        unit_str = obs.temperature_unit or ""

        if min_temp is None or max_temp is None:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
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
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
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

        # 5. Target temperature step & grid origin check
        raw_step = obs.attributes.get("target_temp_step")
        if raw_step is None and obs.target_temp_step is None:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="step_unknown",
                reasons=["Entity does not report target_temp_step metadata; step compatibility cannot be reliably established."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        if (
            obs.target_temp_step is None
            or obs.target_temp_step <= 0
            or not math.isfinite(obs.target_temp_step)
            or isinstance(raw_step, bool)
        ):
            display_step = raw_step if raw_step is not None else obs.target_temp_step
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="step_unknown",
                reasons=[f"Entity reported invalid target_temp_step ({display_step!r}); step metadata must be a positive, finite number."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # Step is valid. Check grid origin configuration.
        if grid_config_error:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="step_alignment_unknown",
                reasons=[f"Server-side climate grid origin configuration error: {grid_config_error}"],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        if effective_origin is None:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=None,
                grid_origin_unit=None,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="step_alignment_unknown",
                reasons=[
                    f"Entity reports target_temp_step={obs.target_temp_step}, but no trusted server-side step-grid origin is configured for '{clean_id}' "
                    "(HOME_ASSISTANT_CLIMATE_GRID_ORIGINS). Feasibility planner cannot verify step alignment without operator-verified grid origin configuration."
                ],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        if effective_origin_unit != obs.temperature_unit:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status="step_alignment_unknown",
                reasons=[
                    f"Configured grid origin unit '{effective_origin_unit}' does not match entity's reported temperature unit '{obs.temperature_unit}'."
                ],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        is_step_ok, step_status, step_reason = validate_temperature_step(
            requested_temperature=requested_temperature,
            step=obs.target_temp_step,
            raw_step=raw_step,
            grid_origin=effective_origin,
        )
        if not is_step_ok:
            return ClimateSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                requested_temperature=requested_temperature,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                target_temperature=obs.target_temperature,
                min_temp=min_temp,
                max_temp=max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=False,
                feasibility_status=step_status,
                reasons=[step_reason] if step_reason else ["Target temperature step validation failed."],
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # All checks passed: plan is ready (feasibility assessment only, no mutation)
        origin_desc = f" from trusted server-configured grid origin {effective_origin}{effective_origin_unit}"
        return ClimateSetpointPlan(
            plan_id=plan_id,
            entity_id=clean_id,
            requested_temperature=requested_temperature,
            requested_unit=requested_unit,
            grid_origin=effective_origin,
            grid_origin_unit=effective_origin_unit,
            current_temperature=obs.current_temperature,
            target_temperature=obs.target_temperature,
            min_temp=min_temp,
            max_temp=max_temp,
            target_temp_step=obs.target_temp_step,
            supported_features=obs.supported_features,
            temperature_unit=obs.temperature_unit,
            hvac_mode=obs.hvac_mode,
            hvac_modes=obs.hvac_modes,
            is_feasible=True,
            feasibility_status="ready",
            reasons=[
                f"Requested temperature {requested_temperature}{unit_str} is feasible: aligns with reported step ({obs.target_temp_step}{unit_str}){origin_desc}, "
                f"falls within operating bounds [{min_temp}, {max_temp}]{unit_str}, entity advertises single-setpoint capability (features={obs.supported_features}), "
                f"and active mode '{obs.hvac_mode}' supports temperature regulation. "
                "Guarantees metadata feasibility only; does not claim physical device connectivity or guarantee subsequent actuation success."
            ],
            is_mutation_performed=False,
            input_digest=input_digest,
            data_classification=obs.data_classification,
        )

    async def assess_dual_setpoint_feasibility(
        self,
        target_entity_id: str,
        target_temp_low: float,
        target_temp_high: float,
        requested_unit: str | None = None,
    ) -> ClimateDualSetpointPlan:
        """Assess the feasibility of a requested dual-setpoint temperature range without executing mutations.

        A plan is an assessment only. It is not an approval ticket, executable command,
        or authorization to actuate the device. Physical mutation is strictly forbidden.
        """
        clean_id = target_entity_id.strip() if isinstance(target_entity_id, str) else str(target_entity_id)
        plan_id = f"plan_climate_dual_{uuid.uuid4().hex[:12]}"

        # Resolve effective server-configured grid origin for this entity
        effective_origin: float | None = None
        effective_origin_unit: str | None = None
        grid_config_error: str | None = None

        try:
            current_allowlist = validate_climate_allowlist()
            origins_map = validate_climate_grid_origins(
                raw_config=self._trusted_grid_origins_raw,
                allowlist=current_allowlist,
            )
            if clean_id in origins_map:
                effective_origin = origins_map[clean_id]["origin"]
                effective_origin_unit = origins_map[clean_id]["unit"]
        except ValidationError as exc:
            grid_config_error = str(exc)
        except Exception as exc:
            grid_config_error = f"Failed to load trusted grid origin configuration: {exc}"

        # Canonical SHA-256 digest binding exact entity ID, target_temp_low, target_temp_high, requested unit, and effective origin + unit
        origin_digest_str = f"{effective_origin}:{effective_origin_unit}" if effective_origin is not None else "none:none"
        digest_input = f"{clean_id}:{target_temp_low}:{target_temp_high}:{requested_unit or ''}:{origin_digest_str}"
        input_digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()

        # Reject non-finite, boolean, or malformed numeric inputs
        is_low_valid = (
            isinstance(target_temp_low, (int, float))
            and not isinstance(target_temp_low, bool)
            and math.isfinite(target_temp_low)
        )
        is_high_valid = (
            isinstance(target_temp_high, (int, float))
            and not isinstance(target_temp_high, bool)
            and math.isfinite(target_temp_high)
        )

        safe_low = target_temp_low if is_low_valid else float("nan")
        safe_high = target_temp_high if is_high_valid else float("nan")

        if not is_low_valid or not is_high_valid:
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=safe_low,
                target_temp_high=safe_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                is_feasible=False,
                feasibility_status="state_unknown",
                reasons=["Requested lower and upper temperatures must be finite numeric values."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Range ordering check: lower target must be strictly less than upper target
        if target_temp_low >= target_temp_high:
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                is_feasible=False,
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested lower target temperature ({target_temp_low}) must be strictly less than upper target temperature ({target_temp_high})."
                ],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Integration & climate awareness enablement checks
        if not settings.HOME_ASSISTANT_ENABLED:
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                is_feasible=False,
                feasibility_status="disabled",
                reasons=["Home Assistant integration is disabled by policy (HOME_ASSISTANT_ENABLED=false)."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        if not getattr(settings, "HOME_ASSISTANT_CLIMATE_AWARENESS_ENABLED", False):
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
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
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                is_feasible=False,
                feasibility_status="not_allowlisted",
                reasons=[str(exc)],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        if clean_id not in allowlist:
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                is_feasible=False,
                feasibility_status="not_allowlisted",
                reasons=[f"Target entity '{clean_id}' is not in HOME_ASSISTANT_CLIMATE_ALLOWLIST."],
                is_mutation_performed=False,
                input_digest=input_digest,
            )

        # Observe entity state (read-only GET)
        obs = await self.get_climate_observation(clean_id)

        # Helper to construct plan with current observation attributes
        def make_plan(feasibility_status: str, reasons: list[str], is_feasible: bool = False) -> ClimateDualSetpointPlan:
            return ClimateDualSetpointPlan(
                plan_id=plan_id,
                entity_id=clean_id,
                target_temp_low=target_temp_low,
                target_temp_high=target_temp_high,
                requested_unit=requested_unit,
                grid_origin=effective_origin,
                grid_origin_unit=effective_origin_unit,
                current_temperature=obs.current_temperature,
                current_target_temp_low=obs.target_temp_low,
                current_target_temp_high=obs.target_temp_high,
                current_target_temperature=obs.target_temperature,
                min_temp=obs.min_temp,
                max_temp=obs.max_temp,
                target_temp_step=obs.target_temp_step,
                supported_features=obs.supported_features,
                temperature_unit=obs.temperature_unit,
                hvac_mode=obs.hvac_mode,
                hvac_modes=obs.hvac_modes,
                is_feasible=is_feasible,
                feasibility_status=feasibility_status,
                reasons=reasons,
                is_mutation_performed=False,
                input_digest=input_digest,
                data_classification=obs.data_classification,
            )

        # If observation is not ready, inherit the honest uncertainty status
        if obs.state_readiness != "ready":
            return make_plan(
                feasibility_status=obs.state_readiness,
                reasons=obs.reasons or [f"Entity is not ready for planning ({obs.state_readiness})."],
            )

        # 1. Dual-setpoint capability check (supported_features)
        raw_feat = obs.attributes.get("supported_features")
        is_cap_ok, cap_status, cap_reason = validate_dual_temperature_capability(
            supported_features=obs.supported_features,
            raw_supported_features=raw_feat,
        )
        if not is_cap_ok:
            return make_plan(
                feasibility_status=cap_status,
                reasons=[cap_reason] if cap_reason else ["Capability validation failed."],
            )

        # 2. Operating mode compatibility check (must be 'heat_cool'; planner never switches mode)
        is_mode_ok, mode_status, mode_reason = validate_dual_mode_compatibility(obs.hvac_mode)
        if not is_mode_ok:
            return make_plan(
                feasibility_status=mode_status,
                reasons=[mode_reason] if mode_reason else ["Operating mode is incompatible with dual-setpoint planning."],
            )

        # 3. Check unit consistency
        if requested_unit is not None:
            norm_requested_unit = parse_temperature_unit(requested_unit)
            if norm_requested_unit is None:
                return make_plan(
                    feasibility_status="unit_unknown",
                    reasons=[f"Requested temperature unit '{requested_unit}' is unrecognized or unsupported."],
                )
            if norm_requested_unit != obs.temperature_unit:
                return make_plan(
                    feasibility_status="unit_unknown",
                    reasons=[
                        f"Requested unit '{requested_unit}' conflicts with entity's reported unit '{obs.temperature_unit}'."
                    ],
                )

        # 4. Validate temperature limits for both low and high
        min_temp = obs.min_temp
        max_temp = obs.max_temp
        unit_str = obs.temperature_unit or ""

        if min_temp is None or max_temp is None:
            return make_plan(
                feasibility_status="state_unknown",
                reasons=["Missing supported minimum or maximum temperature bounds."],
            )

        if target_temp_low < min_temp:
            return make_plan(
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested lower temperature {target_temp_low}{unit_str} is below minimum supported setpoint {min_temp}{unit_str}."
                ],
            )

        if target_temp_low > max_temp:
            return make_plan(
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested lower temperature {target_temp_low}{unit_str} is above maximum supported setpoint {max_temp}{unit_str}."
                ],
            )

        if target_temp_high < min_temp:
            return make_plan(
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested upper temperature {target_temp_high}{unit_str} is below minimum supported setpoint {min_temp}{unit_str}."
                ],
            )

        if target_temp_high > max_temp:
            return make_plan(
                feasibility_status="target_out_of_range",
                reasons=[
                    f"Requested upper temperature {target_temp_high}{unit_str} is above maximum supported setpoint {max_temp}{unit_str}."
                ],
            )

        # 5. Target temperature step & grid origin check
        raw_step = obs.attributes.get("target_temp_step")
        if raw_step is None and obs.target_temp_step is None:
            return make_plan(
                feasibility_status="step_unknown",
                reasons=["Entity does not report target_temp_step metadata; step compatibility cannot be reliably established."],
            )

        if (
            obs.target_temp_step is None
            or obs.target_temp_step <= 0
            or not math.isfinite(obs.target_temp_step)
            or isinstance(raw_step, bool)
        ):
            display_step = raw_step if raw_step is not None else obs.target_temp_step
            return make_plan(
                feasibility_status="step_unknown",
                reasons=[f"Entity reported invalid target_temp_step ({display_step!r}); step metadata must be a positive, finite number."],
            )

        # Step is valid. Check grid origin configuration.
        if grid_config_error:
            return make_plan(
                feasibility_status="step_alignment_unknown",
                reasons=[f"Server-side climate grid origin configuration error: {grid_config_error}"],
            )

        if effective_origin is None:
            return make_plan(
                feasibility_status="step_alignment_unknown",
                reasons=[
                    f"Entity reports target_temp_step={obs.target_temp_step}, but no trusted server-side step-grid origin is configured for '{clean_id}' "
                    "(HOME_ASSISTANT_CLIMATE_GRID_ORIGINS). Feasibility planner cannot verify step alignment without operator-verified grid origin configuration."
                ],
            )

        if effective_origin_unit != obs.temperature_unit:
            return make_plan(
                feasibility_status="step_alignment_unknown",
                reasons=[
                    f"Configured grid origin unit '{effective_origin_unit}' does not match entity's reported temperature unit '{obs.temperature_unit}'."
                ],
            )

        # Validate alignment of target_temp_low
        is_step_low_ok, step_low_status, step_low_reason = validate_temperature_step(
            requested_temperature=target_temp_low,
            step=obs.target_temp_step,
            raw_step=raw_step,
            grid_origin=effective_origin,
        )
        if not is_step_low_ok:
            return make_plan(
                feasibility_status=step_low_status,
                reasons=[step_low_reason] if step_low_reason else ["Lower target temperature step validation failed."],
            )

        # Validate alignment of target_temp_high
        is_step_high_ok, step_high_status, step_high_reason = validate_temperature_step(
            requested_temperature=target_temp_high,
            step=obs.target_temp_step,
            raw_step=raw_step,
            grid_origin=effective_origin,
        )
        if not is_step_high_ok:
            return make_plan(
                feasibility_status=step_high_status,
                reasons=[step_high_reason] if step_high_reason else ["Upper target temperature step validation failed."],
            )

        # All checks passed: dual-setpoint plan is ready (feasibility assessment only, no mutation)
        origin_desc = f" from trusted server-configured grid origin {effective_origin}{effective_origin_unit}"
        return make_plan(
            is_feasible=True,
            feasibility_status="ready",
            reasons=[
                f"Requested dual-setpoint temperature range [{target_temp_low}, {target_temp_high}]{unit_str} is feasible: "
                f"both targets align with reported step ({obs.target_temp_step}{unit_str}){origin_desc}, "
                f"fall within operating bounds [{min_temp}, {max_temp}]{unit_str}, entity advertises dual-setpoint range capability (features={obs.supported_features}), "
                f"and active mode '{obs.hvac_mode}' supports dual-setpoint regulation. "
                "Guarantees metadata feasibility only; does not claim physical device connectivity or guarantee subsequent actuation success."
            ],
        )
