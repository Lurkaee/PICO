import base64
import struct
import pytest
from pico.core.errors import AuthorizationError, ValidationError
from pico.perception.contracts import Modality, VisionCaptureRequest
from pico.perception.vision.adapter import VisionAdapter, parse_image_dimensions


def make_dummy_png(width: int = 120, height: int = 80) -> bytes:
    """Generate minimal valid PNG header."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr_type = b"IHDR"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack(">I", 0)
    return header + ihdr_len + ihdr_type + ihdr_data + ihdr_crc + b"\x00" * 32


def make_dummy_ppm(width: int = 64, height: int = 64) -> bytes:
    """Generate minimal valid PPM (P6) header with pixel data."""
    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    pixels = b"\x80" * (width * height * 3)
    return header + pixels


def test_image_dimension_parsers() -> None:
    # 1. Test PNG parser
    png_bytes = make_dummy_png(320, 240)
    w, h = parse_image_dimensions(png_bytes, "png")
    assert w == 320
    assert h == 240

    # 2. Test PPM parser
    ppm_bytes = make_dummy_ppm(64, 48)
    w, h = parse_image_dimensions(ppm_bytes, "ppm")
    assert w == 64
    assert h == 48


@pytest.mark.anyio
async def test_vision_snapshot_lifecycle_and_consent() -> None:
    adapter = VisionAdapter()

    # 1. Camera is off by default
    status = adapter.get_status()
    assert status["camera_available"] is False
    assert status["ocr_available"] is False

    png_bytes = make_dummy_png(200, 150)
    png_b64 = base64.b64encode(png_bytes).decode("ascii")

    # 2. Missing consent must fail closed with AuthorizationError
    req_no_consent = VisionCaptureRequest(
        user_consent=False,
        image_base64=png_b64,
        image_format="png",
    )
    with pytest.raises(AuthorizationError):
        await adapter.ingest_snapshot(req_no_consent)

    # 3. Explicit consent allows snapshot ingestion
    req = VisionCaptureRequest(
        user_consent=True,
        image_base64=png_b64,
        image_format="png",
        source_device="cam:webcam0",
        purpose="desk_status",
    )
    response, event = await adapter.ingest_snapshot(req)

    assert response.width == 200
    assert response.height == 150
    assert response.format == "png"
    assert response.data_classification == "INTERNAL"
    assert response.confidence == 0.0
    assert response.raw_buffer_retained is False

    # Event contract checks - metadata-only validated without false scene analysis claims
    assert event.modality == Modality.VISION
    assert event.event_type == "visual_snapshot_ingested"
    assert event.confidence == 0.0
    assert event.structured_data["metadata_only"] is True
    assert event.structured_data["scene_analysis_performed"] is False
    assert event.raw_payload_scrubbed is True
    assert event.source_device_id == "cam:webcam0"
    assert "desk_status" in event.provenance


@pytest.mark.anyio
async def test_vision_payload_bounds() -> None:
    adapter = VisionAdapter()

    # 1. Empty payload rejected
    with pytest.raises(ValidationError):
        await adapter.ingest_snapshot(
            VisionCaptureRequest(user_consent=True, image_base64="", image_format="png")
        )

    # 2. Oversized image (> 5 MB) rejected
    oversized = b"\x00" * (5 * 1024 * 1024 + 1024)
    oversized_b64 = base64.b64encode(oversized).decode("ascii")
    with pytest.raises(ValidationError) as exc:
        await adapter.ingest_snapshot(
            VisionCaptureRequest(user_consent=True, image_base64=oversized_b64, image_format="png")
        )
    assert "exceeds limit" in str(exc.value)

    # 3. Oversized dimensions (> 4096px) rejected
    huge_png = make_dummy_png(5000, 3000)
    huge_b64 = base64.b64encode(huge_png).decode("ascii")
    with pytest.raises(ValidationError) as exc_dim:
        await adapter.ingest_snapshot(
            VisionCaptureRequest(user_consent=True, image_base64=huge_b64, image_format="png")
        )
    assert "exceed maximum allowed dimension" in str(exc_dim.value)
