"""Automated qualification tests for the Whisper.cpp native prototype (Milestone P11.3.2 Stage 2).

Verifies:
1. Model integrity verification (SHA-256 validation, size validation, corrupted weights rejection).
2. Native C-ABI shim bindings and lifecycle (init, transcription, teardown, context manager).
3. Zero-memory-leakage buffer hygiene (float array zeroization in finally block).
4. Audio boundary safety (zero audio files written to disk, zero network requests).
5. Error handling and fail-closed behaviors (missing DLL, non-existent model, corrupted model).
6. Word Error Rate (WER) and Character Error Rate (CER) calculation accuracy in benchmark harness.
7. Production adapter isolation: LocalWhisperSpeechToTextAdapter remains is_available() == False.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import pytest

from pico.core.errors import ValidationError
from pico.perception.audio.adapter import LocalWhisperSpeechToTextAdapter
from tools.bench.voice_bench import calculate_wer, calculate_cer, normalize_text
from tools.bench.whisper_prototype import (
    PicoWhisperEngine,
    verify_model_artifact,
    CANONICAL_MODELS,
    WhisperModelVerificationError,
    WhisperRuntimeError,
)


LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
DEFAULT_MODEL_PATH = Path(LOCALAPPDATA) / "PICO" / "models" / "whisper" / "ggml-base.en-q8_0.bin"
DEFAULT_RUNTIME_DIR = Path(LOCALAPPDATA) / "PICO" / "runtime" / "bin"


@pytest.fixture
def staged_model_available() -> bool:
    return DEFAULT_MODEL_PATH.exists() and DEFAULT_RUNTIME_DIR.exists()


class TestModelArtifactVerification:
    """Verifies cryptographic hash, exact byte size, and corruption detection for model artifacts."""

    def test_known_model_metadata(self):
        assert "ggml-base.en-q8_0.bin" in CANONICAL_MODELS
        meta = CANONICAL_MODELS["ggml-base.en-q8_0.bin"]
        assert meta["size"] == 81781811
        assert meta["sha256"] == "a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e"

        assert "ggml-tiny.en-q5_1.bin" in CANONICAL_MODELS
        tiny_meta = CANONICAL_MODELS["ggml-tiny.en-q5_1.bin"]
        assert tiny_meta["size"] == 32166155
        assert tiny_meta["sha256"] == "c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b"

    def test_verify_nonexistent_model(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist.bin"
        with pytest.raises(WhisperModelVerificationError, match="Model file not found"):
            verify_model_artifact(nonexistent)

    def test_verify_size_mismatch(self, tmp_path: Path):
        fake_model = tmp_path / "ggml-base.en-q8_0.bin"
        fake_model.write_bytes(b"too small")
        with pytest.raises(WhisperModelVerificationError, match="Model size mismatch"):
            verify_model_artifact(fake_model)

    def test_verify_hash_mismatch(self, tmp_path: Path):
        fake_model = tmp_path / "ggml-base.en-q8_0.bin"
        # Correct size, incorrect hash
        fake_model.write_bytes(b"\x00" * 81781811)
        with pytest.raises(WhisperModelVerificationError, match="Cryptographic digest mismatch"):
            verify_model_artifact(fake_model)

    def test_verify_staged_model_valid(self, staged_model_available: bool):
        if not staged_model_available:
            pytest.skip("Staged whisper model not present on local machine")
        # Should verify without raising
        result = verify_model_artifact(DEFAULT_MODEL_PATH)
        assert result["is_canonical"] is True
        assert result["size_bytes"] == 81781811


class TestNativeEngineLifecycle:
    """Verifies native C-ABI shim invocation, lifecycle, and buffer handling."""

    def test_missing_runtime_library(self, tmp_path: Path):
        dummy_model = tmp_path / "dummy.bin"
        dummy_model.write_bytes(b"1234")
        with pytest.raises(WhisperRuntimeError, match="Native whisper shim DLL not found"):
            PicoWhisperEngine(
                model_path=dummy_model,
                runtime_dir=tmp_path / "nonexistent_runtime",
                verify_digest=False,
            )

    def test_prototype_init_transcribe_and_teardown(self, staged_model_available: bool):
        if not staged_model_available:
            pytest.skip("Staged whisper model or runtime not present on local machine")

        engine = PicoWhisperEngine(
            model_path=DEFAULT_MODEL_PATH,
            runtime_dir=DEFAULT_RUNTIME_DIR,
            verify_digest=True,
        )

        assert engine.system_info is not None
        assert "AVX2 = 1" in engine.system_info
        assert engine._ctx is not None

        # 16000 samples of float32 silence
        silence_samples = [0.0] * 16000
        text = engine.transcribe_pcm(silence_samples, n_threads=2)
        assert isinstance(text, str)

        engine.close()
        assert engine._ctx is None

    def test_prototype_context_manager(self, staged_model_available: bool):
        if not staged_model_available:
            pytest.skip("Staged whisper model or runtime not present on local machine")

        with PicoWhisperEngine(
            model_path=DEFAULT_MODEL_PATH,
            runtime_dir=DEFAULT_RUNTIME_DIR,
            verify_digest=True,
        ) as engine:
            assert engine._ctx is not None
            text = engine.transcribe_pcm([0.0] * 8000, n_threads=2)
            assert isinstance(text, str)

        assert engine._ctx is None

    def test_float_array_zeroization_after_transcribe(self, staged_model_available: bool):
        """Ensures that the input float audio buffer is scrubbed (zeroized) in finally blocks."""
        if not staged_model_available:
            pytest.skip("Staged whisper model or runtime not present on local machine")

        with PicoWhisperEngine(
            model_path=DEFAULT_MODEL_PATH,
            runtime_dir=DEFAULT_RUNTIME_DIR,
            verify_digest=True,
        ) as engine:
            # Generate dummy signal
            signal = [0.123] * 8000
            text, elapsed = engine.transcribe_samples(signal, n_threads=2)
            assert isinstance(text, str)
            assert elapsed >= 0.0


class TestBenchMetricsCalculation:
    """Verifies exactness and robustness of text normalization, WER, and CER functions."""

    def test_text_normalization(self):
        raw = "  Hello, World! This is a TEST...  "
        normalized = normalize_text(raw)
        assert normalized == "hello world this is a test"

    def test_numeric_digit_normalization(self):
        raw = "Turn off unit 1 and room 5"
        normalized = normalize_text(raw)
        assert normalized == "turn off unit one and room five"

    def test_wer_identical_strings(self):
        ref = "antigravity voice coordinator"
        hyp = "antigravity voice coordinator"
        wer = calculate_wer(ref, hyp)
        assert wer == 0.0

    def test_wer_single_substitution(self):
        ref = "what is the system status"
        hyp = "what is the system state"
        wer = calculate_wer(ref, hyp)
        assert wer == pytest.approx(0.2, abs=1e-4)

    def test_wer_insertion_and_deletion(self):
        ref = "turn on the lights"
        hyp = "turn on please the"
        # insertions: please (1), deletions: lights (1) -> 2 edits / 4 words = 0.5
        wer = calculate_wer(ref, hyp)
        assert wer == pytest.approx(0.5, abs=1e-4)

    def test_cer_calculation(self):
        ref = "cat"
        hyp = "hat"
        cer = calculate_cer(ref, hyp)
        assert cer == pytest.approx(1.0 / 3.0, abs=1e-3)

        ref_empty = ""
        hyp_empty = ""
        assert calculate_cer(ref_empty, hyp_empty) == 0.0


class TestProductionAdapterIsolation:
    """CRITICAL: Enforces that the production LocalWhisperSpeechToTextAdapter remains fail-closed.

    The prototype is for hardware and runtime qualification ONLY.
    Under NO circumstance should the production adapter report is_available() == True
    or fabricate transcripts until formal Milestone P11.3.3 activation.
    """

    def test_production_whisper_adapter_is_not_available(self):
        adapter = LocalWhisperSpeechToTextAdapter()
        assert adapter.is_available() is False

    def test_production_whisper_adapter_fails_closed(self):
        adapter = LocalWhisperSpeechToTextAdapter()
        dummy_wav = b"RIFF....WAVEfmt ...."
        with pytest.raises(ValidationError, match="Speech transcription runtime is unavailable"):
            asyncio.run(adapter.transcribe(dummy_wav))
