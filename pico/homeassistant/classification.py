from typing import Any
from pico.homeassistant.contracts import EntityState
from pico.schemas.context import ContextItem

VALID_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}
CLASSIFICATION_SEVERITY = {"PUBLIC": 0, "INTERNAL": 1, "SENSITIVE": 2, "SECRET": 3}

# Domains that represent household security, physical access, occupancy, or presence
SENSITIVE_DOMAINS = {
    "lock",
    "alarm_control_panel",
    "camera",
    "person",
    "device_tracker",
    "cover",
}

# Binary sensor device classes that represent entry points, security, or presence
SENSITIVE_DEVICE_CLASSES = {
    "door",
    "window",
    "garage_door",
    "lock",
    "tamper",
    "safety",
    "motion",
    "occupancy",
    "presence",
}


def compute_trusted_ha_classification(
    entity_id: str,
    domain: str,
    attributes: dict[str, Any] | None = None,
    caller_classification: str | None = None,
) -> str:
    """Compute trusted data classification for Home Assistant observations.

    Non-negotiable invariants:
    1. Household data is strictly private by default (minimum INTERNAL).
    2. Sensitive domains (locks, alarms, cameras, presence, entry points) are SENSITIVE.
    3. Callers have ZERO authority to downgrade home observations to PUBLIC.
    4. Missing, unknown, invalid, or PUBLIC inputs fail closed to INTERNAL or SENSITIVE.
    5. Elevated caller classifications (SENSITIVE or SECRET) are accepted and preserved.
    """
    dom = domain.strip().lower() if domain else ""
    attrs = attributes or {}
    device_class = str(attrs.get("device_class", "")).strip().lower()

    # Determine baseline minimum sensitivity for this entity
    is_sensitive = (
        dom in SENSITIVE_DOMAINS
        or (dom == "binary_sensor" and device_class in SENSITIVE_DEVICE_CLASSES)
        or any(k in entity_id.lower() for k in ("door", "window", "lock", "alarm", "camera", "person"))
    )

    baseline_class = "SENSITIVE" if is_sensitive else "INTERNAL"
    baseline_sev = CLASSIFICATION_SEVERITY[baseline_class]

    if not caller_classification:
        return baseline_class

    upper = caller_classification.strip().upper()
    if upper not in CLASSIFICATION_SEVERITY:
        # Invalid / unknown classification fails closed to baseline
        return baseline_class

    caller_sev = CLASSIFICATION_SEVERITY[upper]

    # Invariant: Caller cannot downgrade below baseline severity
    effective_sev = max(baseline_sev, caller_sev)

    for name, sev in CLASSIFICATION_SEVERITY.items():
        if sev == effective_sev:
            return name

    return baseline_class


def format_homeassistant_context_items(
    entities: list[EntityState], max_items: int = 5
) -> list[ContextItem]:
    """Compile recent Home Assistant entity states into structured ContextItem objects.

    Invariants:
    1. Concise structured observations: Never dump raw attributes or tokens into prompt.
    2. Lower priority than mandatory instructions: Priority 45 ensures home state is pruned
       before mandatory safety rules (100) or current user prompt (90).
    3. Classification preservation: Always reflects computed trusted classification.
    4. Explicit provenance: Retains 'ha:<entity_id>' provenance tag.
    """
    items: list[ContextItem] = []

    for entity in entities[:max_items]:
        name = entity.friendly_name or entity.entity_id
        unit = f" {entity.unit_of_measurement}" if entity.unit_of_measurement else ""
        content = f"Smart Home ({entity.entity_id}): {name} is reported '{entity.state}{unit}'."

        item = ContextItem(
            id=f"ctx_ha_{entity.entity_id.replace('.', '_')}",
            item_type="sensor_event",
            role="system",
            content=content,
            source_provenance=f"ha:{entity.entity_id}",
            data_classification=entity.data_classification,
            priority=45,
            is_mandatory=False,
            metadata={
                "entity_id": entity.entity_id,
                "domain": entity.domain,
                "state": entity.state,
            },
        )
        items.append(item)

    return items
