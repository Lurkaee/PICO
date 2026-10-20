from pico.perception.contracts import Modality, NormalizedPerceptionEvent
from pico.schemas.context import ContextItem


def format_perception_context_items(
    events: list[NormalizedPerceptionEvent], max_items: int = 3
) -> list[ContextItem]:
    """Compile recent normalized perception events into structured ContextItem objects.

    Invariants:
    1. Concise structured observations: Never dump raw image/audio bytes into prompt.
    2. Lower priority than mandatory rules: Sensor observations (priority 45) are pruned
       before mandatory instructions (priority >= 100) or current turn (priority 90).
    3. Classification preservation: Retains event's data_classification (e.g. INTERNAL).
    4. Explicit provenance: Preserves exact sensor/device provenance tag.
    """
    items: list[ContextItem] = []
    # Take most recent up to max_items
    for ev in events[:max_items]:
        content = ""
        if ev.modality == Modality.AUDIO:
            transcription = ev.structured_data.get("transcription", "")
            content = f"Recent speech transcribed ({ev.source_device_id}): \"{transcription}\""
        elif ev.modality == Modality.VISION:
            desc = ev.structured_data.get("scene_description", "")
            content = f"Visual observation ({ev.source_device_id}): {desc}"
        elif ev.modality == Modality.MOTION:
            if ev.event_type == "movement_interpreted":
                dir_val = ev.structured_data.get("direction", "stationary")
                mag_val = ev.structured_data.get("magnitude", "none")
                content = f"Movement detected ({ev.source_device_id}): Direction={dir_val}, Magnitude={mag_val}."
            else:
                detected = ev.structured_data.get("motion_detected", True)
                content = f"Motion sensor ({ev.source_device_id}): Motion {'detected' if detected else 'cleared'}."
        elif ev.modality == Modality.ENVIRONMENTAL:
            readings = ev.structured_data.get("readings", {})
            parts = [f"{k}={v}" for k, v in readings.items() if v is not None]
            reading_str = ", ".join(parts) if parts else "telemetry logged"
            content = f"Environmental sensor ({ev.source_device_id}): {reading_str}."
        else:
            content = f"Perception observation ({ev.modality.value}): {ev.event_type}."

        item = ContextItem(
            id=f"ctx_{ev.event_id}",
            item_type="sensor_event",
            role="system",
            content=content,
            source_provenance=ev.provenance,
            data_classification=ev.data_classification,
            priority=45,  # Lower than mandatory instructions (100) and user turns (90)
            is_mandatory=False,
            metadata={
                "event_id": ev.event_id,
                "modality": ev.modality.value if hasattr(ev.modality, "value") else str(ev.modality),
                "confidence": ev.confidence,
            },
        )
        items.append(item)

    return items
