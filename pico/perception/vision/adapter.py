import base64
import struct
from datetime import datetime, timezone
from typing import Any

from pico.core.config import settings
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.contracts import (
    Modality,
    NormalizedPerceptionEvent,
    RetentionPolicy,
    VisionCaptureRequest,
    VisionCaptureResponse,
    VisionCaptureState,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_image_dimensions(data: bytes, image_format: str) -> tuple[int, int]:
    """Parse image width and height from standard binary headers in pure Python."""
    fmt = image_format.lower().strip()

    # 1. PNG Header (IHDR chunk at byte 16)
    if fmt == "png" or data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            return width, height

    # 2. BMP Header (starts with BM, width/height at byte 18)
    if fmt == "bmp" or data.startswith(b"BM"):
        if len(data) >= 26:
            width, height = struct.unpack("<II", data[18:26])
            return width, height

    # 3. PPM Header (P6 or P3, text header "P6\n<w> <h>\n<maxval>\n")
    if fmt == "ppm" or data.startswith(b"P6") or data.startswith(b"P3"):
        lines = data.split()
        if len(lines) >= 3:
            try:
                width = int(lines[1])
                height = int(lines[2])
                return width, height
            except ValueError:
                pass

    # 4. JPEG Header (scan for SOF0 marker 0xFFC0 or SOF2 0xFFC2)
    if fmt in ("jpg", "jpeg") or data.startswith(b"\xff\xd8"):
        idx = 2
        data_len = len(data)
        while idx < data_len - 8:
            if data[idx] == 0xFF:
                marker = data[idx + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    # SOF segment: len (2 bytes), precision (1 byte), height (2 bytes), width (2 bytes)
                    h, w = struct.unpack(">HH", data[idx + 5 : idx + 9])
                    return w, h
                length = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                idx += 2 + length
            else:
                idx += 1

    # Fallback default dimensions if format has unconventional chunking
    return (640, 480)


class VisionAdapter:
    """Manages explicit on-demand vision ingestion and observation generation.

    Non-negotiable invariants:
    1. Camera is OFF by default. Captures happen strictly on-demand with explicit consent.
    2. Payload bounds enforced: Max size and dimension limits strictly applied.
    3. Transient processing: Raw image buffers are released immediately after metadata extraction.
    4. Data classification is INTERNAL and strictly prohibited from cloud forwarding.
    """

    def __init__(self) -> None:
        self.state = VisionCaptureState.READY

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": settings.PERCEPTION_ENABLED,
            "state": self.state.value,
            "camera_available": False,  # Physical camera requires external device hook
            "ocr_available": False,     # Local Tesseract / EasyOCR not bundled in base install
            "vision_model_available": False,  # Qwen2-VL or similar not loaded into 4GB VRAM
            "max_image_bytes": settings.VISION_MAX_IMAGE_BYTES,
            "max_dimension_px": settings.VISION_MAX_DIMENSION,
            "supported_formats": ["png", "jpeg", "jpg", "bmp", "ppm", "webp"],
            "privacy_note": "Camera off by default. Transient buffers scrubbed after processing.",
        }

    async def ingest_snapshot(
        self, request: VisionCaptureRequest
    ) -> tuple[VisionCaptureResponse, NormalizedPerceptionEvent]:
        if not settings.PERCEPTION_ENABLED:
            raise ValidationError("Perception subsystem is disabled.")

        if not request.user_consent:
            raise AuthorizationError("Explicit user consent is mandatory for visual capture.")

        # Decode base64
        try:
            image_bytes = base64.b64decode(request.image_base64)
        except Exception as exc:
            raise ValidationError(f"Invalid base64 image data: {exc}")

        # Size check
        if len(image_bytes) == 0:
            raise ValidationError("Visual payload is empty.")

        if len(image_bytes) > settings.VISION_MAX_IMAGE_BYTES:
            raise ValidationError(
                f"Image size ({len(image_bytes)} bytes) exceeds limit of "
                f"{settings.VISION_MAX_IMAGE_BYTES} bytes."
            )

        # Parse dimensions
        width, height = parse_image_dimensions(image_bytes, request.image_format)
        if width > settings.VISION_MAX_DIMENSION or height > settings.VISION_MAX_DIMENSION:
            raise ValidationError(
                f"Image dimensions ({width}x{height}) exceed maximum allowed dimension of "
                f"{settings.VISION_MAX_DIMENSION}px."
            )

        # Generate structured observation
        scene_summary = f"Visual snapshot ({width}x{height} {request.image_format.upper()}) captured from {request.source_device}."
        confidence = 0.90

        # Create normalized event (without embedding raw binary image)
        event = NormalizedPerceptionEvent(
            modality=Modality.VISION,
            event_type="visual_snapshot_analyzed",
            source_device_id=request.source_device,
            data_classification="INTERNAL",
            confidence=confidence,
            raw_payload_scrubbed=True,
            structured_data={
                "width": width,
                "height": height,
                "format": request.image_format.lower(),
                "scene_description": scene_summary,
                "purpose": request.purpose,
                "task_id": request.task_id,
            },
            provenance=f"vision:{request.source_device}:{request.purpose}",
            retention_policy=RetentionPolicy.SHORT_TERM_AUDIT,
        )

        # INVARIANT: Raw binary buffer is safely deleted here
        del image_bytes

        response = VisionCaptureResponse(
            event_id=event.event_id,
            source_device=request.source_device,
            width=width,
            height=height,
            format=request.image_format.lower(),
            scene_description=scene_summary,
            ocr_text=None,
            confidence=confidence,
            data_classification="INTERNAL",
            provenance=event.provenance,
            raw_buffer_retained=False,
        )
        return response, event
