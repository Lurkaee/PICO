import json
import math
from typing import Any

# Disallowed sensitive, credential, or personal attribute keywords
STRIPPED_KEYWORDS = (
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
    "lat",
    "lon",
    "credential",
    "auth",
)

# Strictly approved attribute keys for supported awareness operations
APPROVED_ATTRIBUTE_KEYS = {
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
    "preset_mode",
    "mode",
}


def sanitize_entity_attributes(raw_attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Sanitize and bound entity attributes to prevent leaks of personal info, GPS, and unbounded blobs.

    Rules:
    1. Default to a strict allowlist of explicitly approved attributes for awareness.
    2. Drop unknown attributes regardless of whether their values are numeric, boolean, or strings.
    3. Filter all sensitive location, credential, network, and personal identifiers.
    4. Reject or safely normalize non-finite numeric values (NaN, Inf).
    5. Disallow nested dictionaries or complex object graphs.
    6. Cap string lengths to 128 chars and bounded collections to <= 5 scalar items.
    7. Bound the dictionary to at most 12 keys.
    8. Ensure total UTF-8 encoded JSON size is strictly <= 1024 bytes.
    """
    if not raw_attributes or not isinstance(raw_attributes, dict):
        return {}

    sanitized: dict[str, Any] = {}

    for k, v in raw_attributes.items():
        if not isinstance(k, str):
            continue
        key_lower = k.strip().lower()

        # Reject any stripped/sensitive keyword
        if any(bad in key_lower for bad in STRIPPED_KEYWORDS):
            continue

        # Strictly enforce allowlist: drop all unapproved keys
        if key_lower not in APPROVED_ATTRIBUTE_KEYS:
            continue

        # Nested dictionaries are prohibited
        if isinstance(v, dict):
            continue

        # Handle booleans (bool is a subclass of int, check first)
        if isinstance(v, bool):
            sanitized[k] = v
        elif isinstance(v, int):
            sanitized[k] = v
        elif isinstance(v, float):
            if not math.isfinite(v):
                continue
            sanitized[k] = v
        elif isinstance(v, str):
            sanitized[k] = v[:128]
        elif isinstance(v, (list, tuple)):
            if len(v) <= 5 and all(isinstance(x, (str, int, float, bool)) for x in v):
                clean_list: list[Any] = []
                valid = True
                for x in v:
                    if isinstance(x, float) and not math.isfinite(x):
                        valid = False
                        break
                    clean_list.append(x[:64] if isinstance(x, str) else x)
                if valid:
                    sanitized[k] = clean_list

        if len(sanitized) >= 12:
            break

    # Bound total JSON serialized size
    serialized = json.dumps(sanitized)
    if len(serialized.encode("utf-8")) > 1024:
        minimal: dict[str, Any] = {}
        if "friendly_name" in sanitized:
            minimal["friendly_name"] = str(sanitized["friendly_name"])[:64]
        if "device_class" in sanitized:
            minimal["device_class"] = str(sanitized["device_class"])[:32]
        return minimal

    return sanitized
