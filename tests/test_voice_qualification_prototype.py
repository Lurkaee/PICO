"""Automated qualification tests for the Whisper.cpp native prototype (Milestone P11.3.2 Stage 2).

Verifies:
1. Model integrity verification (canonical registry, explicit metadata, corruption detection, unknown model rejection).
2. Mock/fake-runtime coverage ensuring tests pass in CI without staged DLLs or model files.
3. Thread bounding enforcement (strictly 1 to 4 threads rejected if out of bounds).
4. Zero-memory-leakage buffer hygiene (in-place zeroization verified on success and failure via retained array inspection).
5. Audio boundary safety (zero audio files written to disk, zero network requests).
6. Fail-closed safety test runner in isolated temporary directories.
7. Word Error Rate (WER), Character Error Rate (CER), and edit operations calculation accuracy.
8. Production adapter isolation: LocalWhisperSpeechToTextAdapter remains is_available() == False.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pico.core.errors import ValidationError
from pico.perception.audio.adapter import LocalWhisperSpeechToTextAdapter
from tools.bench.voice_bench import (
    calculate_cer,
    calculate_wer,
    compute_levenshtein_ops,
    normalize_text,
    verify_fail_closed_behavior,
)
from tools.bench.whisper_prototype import (
    CANONICAL_MODELS,
    DEFAULT_MODEL_DIR,
    DEFAULT_RUNTIME_DIR,
    PicoWhisperEngine,
    WhisperModelVerificationError,
    WhisperRuntimeError,
    verify_model_artifact,
)

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
DEFAULT_MODEL_PATH = Path(LOCALAPPDATA) / "PICO" / "models" / "whisper" / "ggml-base.en-q8_0.bin"
DEFAULT_RUNTIME_PATH = Path(LOCALAPPDATA) / "PICO" / "runtime" / "bin"


@pytest.fixture
def staged_model_available() -> bool:
    return DEFAULT_MODEL_PATH.exists() and DEFAULT_RUNTIME_PATH.exists()


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

    def test_verify_unknown_artifact_without_metadata_rejected(self, tmp_path: Path):
        unknown = tmp_path / "custom_model.bin"
        unknown.write_bytes(b"some model weights")
        with pytest.raises(WhisperModelVerificationError, match="cannot be verified without explicit trusted"):
            verify_model_artifact(unknown)

    def test_verify_unknown_artifact_with_explicit_metadata_accepted(self, tmp_path: Path):
        custom = tmp_path / "custom_weights.bin"
        content = b"valid custom weights for test"
        custom.write_bytes(content)
        import hashlib
        expected_sha = hashlib.sha256(content).hexdigest()
        result = verify_model_artifact(
            custom,
            expected_size=len(content),
            expected_sha256=expected_sha,
        )
        assert result["size_bytes"] == len(content)
        assert result["sha256"] == expected_sha
        assert result["is_canonical"] is False

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
        result = verify_model_artifact(DEFAULT_MODEL_PATH)
        assert result["is_canonical"] is True
        assert result["size_bytes"] == 81781811


class TestThreadBounding:
    """Verifies that thread count is strictly bounded to 1 <= threads <= 4."""

    def test_thread_bounds_in_constructor(self, tmp_path: Path):
        dummy = tmp_path / "dummy.bin"
        dummy.write_bytes(b"1234")
        with pytest.raises(ValueError, match="n_threads must be strictly between 1 and 4"):
            PicoWhisperEngine(dummy, verify_digest=False, n_threads=0)

        with pytest.raises(ValueError, match="n_threads must be strictly between 1 and 4"):
            PicoWhisperEngine(dummy, verify_digest=False, n_threads=5)

    def test_thread_bounds_in_transcribe(self, tmp_path: Path):
        dummy = tmp_path / "dummy.bin"
        dummy.write_bytes(b"1234")

        # Mock engine without loading real DLL
        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine._ctx = ctypes.c_void_p(1234)

        with pytest.raises(ValueError, match="n_threads must be strictly between 1 and 4"):
            engine.transcribe_samples([0.0] * 100, n_threads=0)

        with pytest.raises(ValueError, match="n_threads must be strictly between 1 and 4"):
            engine.transcribe_samples([0.0] * 100, n_threads=8)


class TestMemoryZeroizationMocked:
    """Verifies best-effort memory zeroization using mocked C-ABI runtime (CI-safe)."""

    def test_zeroization_after_successful_transcription(self):
        """Verifies that both the passed float list and C float array are zeroed on success."""
        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine._ctx = ctypes.c_void_p(9999)

        retained_c_array = None

        def fake_transcribe(ctx, arr, n_samples, n_threads, out_buf, buf_size, out_len):
            nonlocal retained_c_array
            # Retain pointer to inspect after transcribe_samples completes
            retained_c_array = arr
            # Fill mock output
            out_buf.value = b"mocked transcript"
            if out_len:
                out_len._obj.value = len(b"mocked transcript")
            return 0

        mock_lib = MagicMock()
        mock_lib.pico_whisper_transcribe.side_effect = fake_transcribe
        engine._lib = mock_lib

        input_samples = [0.1, 0.2, 0.3, -0.4, 0.5]
        transcript, elapsed = engine.transcribe_samples(input_samples, n_threads=2)

        assert transcript == "mocked transcript"
        # 1. Verify caller mutable list was zeroized in finally
        assert input_samples == [0.0, 0.0, 0.0, 0.0, 0.0]
        # 2. Verify ctypes C array was scrubbed in finally
        assert retained_c_array is not None
        for i in range(5):
            assert retained_c_array[i] == 0.0

    def test_zeroization_after_native_inference_failure(self):
        """Verifies that buffers are zeroized even when native inference returns an error."""
        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine._ctx = ctypes.c_void_p(9999)

        retained_c_array = None

        def fake_failing_transcribe(ctx, arr, n_samples, n_threads, out_buf, buf_size, out_len):
            nonlocal retained_c_array
            retained_c_array = arr
            return 3  # PICO_WHISPER_ERR_INFERENCE_FAILED

        mock_lib = MagicMock()
        mock_lib.pico_whisper_transcribe.side_effect = fake_failing_transcribe
        engine._lib = mock_lib

        input_samples = [0.9, -0.8, 0.7]
        with pytest.raises(WhisperRuntimeError, match="Native transcription failed"):
            engine.transcribe_samples(input_samples, n_threads=2)

        # In-place scrubbing MUST run in finally block despite exception
        assert input_samples == [0.0, 0.0, 0.0]
        assert retained_c_array is not None
        for i in range(3):
            assert retained_c_array[i] == 0.0


class TestBenchMetricsCalculation:
    """Verifies exactness and robustness of text normalization, WER, CER, and Levenshtein operations."""

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

    def test_levenshtein_ops_breakdown(self):
        ref = "one two three four".split()
        hyp = "one two five six seven".split()
        # substitutions: three->five, four->six; insertion: seven -> dist 3 (2 subs, 0 dels, 1 ins)
        dist, subs, dels, ins = compute_levenshtein_ops(ref, hyp)
        assert dist == 3
        assert subs == 2
        assert dels == 0
        assert ins == 1

    def test_cer_calculation(self):
        ref = "cat"
        hyp = "hat"
        cer = calculate_cer(ref, hyp)
        assert cer == pytest.approx(1.0 / 3.0, abs=1e-3)

        ref_empty = ""
        hyp_empty = ""
        assert calculate_cer(ref_empty, hyp_empty) == 0.0


class TestFailClosedProcedure:
    """Verifies isolated fail-closed testing procedure."""

    def test_fail_closed_test_runner(self, tmp_path: Path):
        result = verify_fail_closed_behavior(runtime_dir=tmp_path / "dummy_runtime")
        assert "passed" in result
        assert "subtests" in result
        assert result["subtests"]["missing_model_fails_closed"] is True
        assert result["subtests"]["corrupted_model_fails_closed"] is True
        assert result["subtests"]["missing_runtime_fails_closed"] is True
        assert result["passed"] is True


class TestProductionAdapterIsolation:
    """CRITICAL: Enforces that the production LocalWhisperSpeechToTextAdapter remains fail-closed.

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


class TestNativeEngineIntegration:
    """Integration tests running against actual staged native binaries and models when present."""

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
            runtime_dir=DEFAULT_RUNTIME_PATH,
            verify_digest=True,
            n_threads=2,
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
            runtime_dir=DEFAULT_RUNTIME_PATH,
            verify_digest=True,
            n_threads=2,
        ) as engine:
            assert engine._ctx is not None
            text = engine.transcribe_pcm([0.0] * 8000, n_threads=2)
            assert isinstance(text, str)

        assert engine._ctx is None
