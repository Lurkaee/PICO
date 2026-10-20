import json
from typing import Any

# Disallowed sensitive or personal attribute keys
STRIPPED_KEYS = {
    "latitude",
    "longitude",
    "altitude",
    "gps_accuracy",
    "radius",
    "user_id",
    "ip_address",
    "mac",
    "email",
    "access_token",
    "token",
    "api_key",
    "password",
    "secret",
    "entity_picture",
}

# Standard safe attribute keys commonly used for household awareness
RETAINED_KEYS = {
    "friendly_name",
    "device_class",
    "unit_of_measurement",
    "battery",
    "battery_level",
    "temperature",
    "current_temperature",
    "target_temperature",
    "humidity",
    "hvac_action",
    "hvac_modes",
    "brightness",
    "color_temp",
    "is_volume_muted",
    "volume_level",
    "current_position",
    "supported_features",
    "preset_mode",
    "mode",
}


def sanitize_entity_attributes(raw_attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Sanitize and bound entity attributes to prevent leaks of personal info, GPS, and unbounded blobs.

    Rules:
    1. Strip all location, user, network, and token keys.
    2. Retain scalar/small values for approved descriptive keys or harmless scalar keys.
    3. Cap string lengths to 128 chars.
    4. Bound the dictionary to at most 12 keys.
    5. Ensure total JSON size is strictly <= 1024 bytes.
    """
    if not raw_attributes or not isinstance(raw_attributes, dict):
        return {}

    sanitized: dict[str, Any] = {}

    for k, v in raw_attributes.items():
        if not isinstance(k, str):
            continue
        key_lower = k.strip().lower()

        # Reject any stripped/sensitive key
        if key_lower in STRIPPED_KEYS or any(bad in key_lower for bad in ("token", "password", "secret", "lat", "lon")):
            continue

        # Keep approved keys or harmless scalar attributes
        if key_lower in RETAINED_KEYS or isinstance(v, (int, float, bool)):
            if isinstance(v, str):
                sanitized[k] = v[:128]
            elif isinstance(v, (int, float, bool)):
                sanitized[k] = v
            elif isinstance(v, (list, tuple)) and len(v) <= 5 and all(isinstance(x, (int, float, str)) for x in v):
                sanitized[k] = [x[:64] if isinstance(x, str) else x for x in v]

        if len(sanitized) >= 12:
            break

    # Bound total size
    serialized = json.dumps(sanitized)
    if len(serialized.encode("utf-8")) > 1024:
        # Fallback to minimal friendly_name and device_class only
        minimal: dict[str, Any] = {}
        if "friendly_name" in sanitized:
            minimal["friendly_name"] = str(sanitized["friendly_name"])[:64]
        if "device_class" in sanitized:
            minimal["device_class"] = str(sanitized["device_class"])[:32]
        return minimal

    return sanitized
