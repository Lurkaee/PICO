from pico.core.errors import ValidationError
from pico.perception.contracts import Modality

VALID_CLASSIFICATIONS = {"PUBLIC", "INTERNAL", "SENSITIVE", "SECRET"}
CLASSIFICATION_SEVERITY = {"PUBLIC": 0, "INTERNAL": 1, "SENSITIVE": 2, "SECRET": 3}

# Modality baseline restrictions: perception is strictly private by default
MODALITY_MINIMUM_CLASSIFICATION: dict[Modality, str] = {
    Modality.AUDIO: "INTERNAL",
    Modality.VISION: "INTERNAL",
    Modality.MOTION: "INTERNAL",
    Modality.ENVIRONMENTAL: "INTERNAL",
}

# Trusted source identity prefixes by modality
TRUSTED_SOURCE_PREFIXES: dict[Modality, tuple[str, ...]] = {
    Modality.AUDIO: ("mic:", "audio:"),
    Modality.VISION: ("cam:", "screen:", "vision:"),
    Modality.MOTION: ("pir:", "cam:", "motion:", "sensor:"),
    Modality.ENVIRONMENTAL: ("sensor:", "esp32:", "mqtt:", "env:"),
}


def validate_trusted_source_device(modality: Modality, source_device: str | None) -> str:
    """Validate that source_device is non-empty and follows trusted identity conventions.

    Fails closed with ValidationError on empty, unknown, or adversarial source IDs.
    """
    if not source_device or not source_device.strip():
        raise ValidationError(f"Missing mandatory source_device for {modality.value} perception.")

    clean_source = source_device.strip()
    lower = clean_source.lower()

    # Reject explicitly adversarial prefixes
    if lower.startswith(("untrusted:", "unknown:", "anonymous:", "public:", "adversary:")):
        raise ValidationError(
            f"Untrusted sensor source identity '{clean_source}'. Perception ingestion fails closed."
        )

    prefixes = TRUSTED_SOURCE_PREFIXES.get(modality, ())
    if not any(lower.startswith(p) for p in prefixes):
        raise ValidationError(
            f"Unrecognized source identifier '{clean_source}' for modality '{modality.value}'. "
            f"Expected trusted prefix from: {list(prefixes)}."
        )

    return clean_source


def compute_trusted_perception_classification(
    modality: Modality,
    source_device: str,
    caller_classification: str | None = None,
    is_sensitive_reading: bool = False,
) -> str:
    """Compute trusted data classification for perception events.

    Non-negotiable invariants:
    1. Caller has ZERO authority to downgrade motion or environmental events to PUBLIC.
    2. Minimum classification for any perception event is INTERNAL.
    3. If caller requests higher sensitivity (SENSITIVE or SECRET), trusted code accepts
       the elevated restriction.
    4. Missing, unknown, invalid, or PUBLIC inputs strictly fail closed to INTERNAL.
    5. Sensitive readings (e.g. door/window contact state, camera captures) default to
       INTERNAL or higher.
    """
    min_class = MODALITY_MINIMUM_CLASSIFICATION.get(modality, "INTERNAL")
    min_sev = CLASSIFICATION_SEVERITY[min_class]

    if not caller_classification:
        if is_sensitive_reading and min_sev < CLASSIFICATION_SEVERITY["SENSITIVE"]:
            return "SENSITIVE"
        return min_class

    upper = caller_classification.strip().upper()
    if upper not in CLASSIFICATION_SEVERITY:
        # Unknown or invalid classification fails closed to minimum
        return "SENSITIVE" if is_sensitive_reading else min_class

    caller_sev = CLASSIFICATION_SEVERITY[upper]

    # Invariant: Never allow caller to downgrade below modality minimum
    # (e.g. caller asking for PUBLIC is clamped to INTERNAL)
    effective_sev = max(min_sev, caller_sev)
    if is_sensitive_reading:
        effective_sev = max(effective_sev, CLASSIFICATION_SEVERITY["INTERNAL"])

    for name, sev in CLASSIFICATION_SEVERITY.items():
        if sev == effective_sev:
            return name

    return "INTERNAL"
