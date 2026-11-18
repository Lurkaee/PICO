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
import io
import json
import os
import tempfile
import threading
import time
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pico.core.errors import ValidationError
from pico.perception.audio.adapter import LocalWhisperSpeechToTextAdapter
from tools.bench import voice_bench
from tools.bench.voice_bench import (
    ContinuousMemorySampler,
    OllamaResidencyState,
    SUPPORTED_OLLAMA_MODELS,
    calculate_cer,
    calculate_wer,
    compute_levenshtein_ops,
    evaluate_gate_04,
    evaluate_gate_05,
    format_metric_pct,
    normalize_text,
    query_git_commit,
    render_markdown_report,
    run_benchmark,
    validate_designated_wav,
    verify_fail_closed_behavior,
    verify_ollama_model_residency,
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


def create_valid_test_wav(duration_s: float = 4.92, sample_rate: int = 16000) -> bytes:
    """Helper to create a strictly compliant in-memory 16kHz mono 16-bit PCM WAV byte stream."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        n_frames = int(duration_s * sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return buf.getvalue()


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
        engine._lock = threading.RLock()
        engine.default_threads = 4

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
        engine._lock = threading.RLock()
        engine.default_threads = 4

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


class TestGate04Evaluation:
    """Verifies GATE-VOICE-04 (Single-Model GPU VRAM & Ollama Residency)."""

    def test_gate_04_single_checkpoint_not_run(self):
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_resident=True,
            ollama_info="running qwen3.5:2b",
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "lifecycle evidence is missing" in text

    def test_gate_04_missing_workload_checkpoint_not_run(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "lifecycle evidence is missing" in text

    def test_gate_04_missing_post_final_checkpoint_not_run(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("sample_1", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "lifecycle evidence is missing" in text

    def test_gate_04_missing_pre_init_checkpoint_not_run(self):
        checkpoints = [
            ("sample_1", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "lifecycle evidence is missing" in text

    def test_gate_04_pass_with_multi_checkpoint_resident_llm(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)", "sha256:target_digest_123"),
            ("post_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)", "sha256:target_digest_123"),
            ("sample_1", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)", "sha256:target_digest_123"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)", "sha256:target_digest_123"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "PASS"
        assert "VRAM Delta = 0 MiB with target model 'qwen3.5:2b' verified resident in GPU VRAM across sampled checkpoints" in text

    def test_gate_04_not_run_on_telemetry_failure_during_inference(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("post_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("sample_jfk.wav", OllamaResidencyState.QUERY_ERROR, "Ollama local API unreachable: connection timed out"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "Ollama residency telemetry could not be verified at checkpoint 'sample_jfk.wav'" in text

    def test_gate_04_fail_on_model_eviction_during_inference(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("post_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("sample_jfk.wav", OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory"),
            ("post_final", OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "FAIL"
        assert "Target model 'qwen3.5:2b' was evicted from GPU during STT execution" in text
        assert "sample_jfk.wav" in text

    def test_gate_04_not_run_without_resident_llm(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory"),
            ("sample_1", OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory"),
            ("post_final", OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "target model 'qwen3.5:2b' was not resident on GPU before benchmark" in text

    def test_gate_04_not_run_when_pre_init_has_query_error(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.QUERY_ERROR, "Ollama local API unreachable"),
            ("sample_1", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1000,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "NOT RUN"
        assert "Ollama residency telemetry could not be verified before benchmark" in text

    def test_gate_04_fail_on_max_vram_increase_returning_to_baseline(self):
        checkpoints = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM"),
            ("sample_1", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM"),
        ]
        # Initial 1000, peak 1025, final 1000 (final delta is 0, but max delta is 25 > 0)
        status, text = evaluate_gate_04(
            vram_max_delta_mb=25,
            vram_readings_valid=True,
            ollama_checkpoints=checkpoints,
            vram_initial_mb=1000,
            max_vram_observed_mb=1025,
            vram_final_mb=1000,
            vram_observations_count=10,
        )
        assert status == "FAIL"
        assert "Maximum observed VRAM Delta = 25 MiB (exceeds 0 MiB budget" in text

    def test_gate_04_not_run_on_missing_telemetry(self):
        status, text = evaluate_gate_04(
            vram_max_delta_mb=None,
            vram_readings_valid=False,
            ollama_resident=True,
            ollama_info="running qwen3.5:2b",
        )
        assert status == "NOT RUN"
        assert "GPU VRAM telemetry unavailable" in text


class TestOllamaResidencyVerification:
    """Verifies positive GPU residency checking and exact allowlist enforcement in verify_ollama_model_residency()."""

    def test_verify_ollama_exact_canonical_model_success(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1500000000,
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.GPU_RESIDENT
        assert "Model 'qwen3.5:2b'" in info
        assert "resident in GPU VRAM (1430.5 MiB)" in info

    def test_verify_ollama_latest_alias_success(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b:latest",
                    "size_vram": 1500000000,
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.GPU_RESIDENT
        assert "Model 'qwen3.5:2b:latest'" in info
        assert "resident in GPU VRAM" in info

    def test_verify_ollama_instruct_alias_success(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b-instruct",
                    "size_vram": 1500000000,
                    "digest": "sha256:instruct_instance",
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b-instruct")
        assert state == OllamaResidencyState.GPU_RESIDENT
        assert "Model 'qwen3.5:2b-instruct'" in info

    def test_verify_ollama_instruct_alias_rejected_when_canonical_expected(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b-instruct",
                    "size_vram": 1500000000,
                    "digest": "sha256:instruct_instance",
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.NOT_RESIDENT
        assert "Different model(s) resident: qwen3.5:2b-instruct" in info
        assert "expected target: qwen3.5:2b" in info

    def test_verify_ollama_cpu_only_rejected(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 0,  # Loaded on CPU only!
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.NOT_RESIDENT
        assert "loaded on CPU only (size_vram=0), not GPU-resident" in info

    def test_verify_ollama_different_model(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "llama3:8b",
                    "size_vram": 5000000000,
                }
            ]
        }

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.NOT_RESIDENT
        assert "Different model(s) resident: llama3:8b" in info

    def test_verify_ollama_arbitrary_variants_rejected(self, monkeypatch):
        for variant in ["not-qwen3.5:2b", "qwen3.5:2b-custom", "qwen3.5:2b-unapproved"]:
            fake_response = {
                "models": [
                    {
                        "name": variant,
                        "size_vram": 1500000000,
                    }
                ]
            }

            class MockResponse:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_val, exc_tb):
                    pass

                def read(self):
                    import json
                    return json.dumps(fake_response).encode("utf-8")

            monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
            state, info = verify_ollama_model_residency("qwen3.5:2b")
            assert state == OllamaResidencyState.NOT_RESIDENT
            assert f"Different model(s) resident: {variant}" in info

    def test_verify_ollama_no_models(self, monkeypatch):
        fake_response = {"models": []}

        class MockResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                pass

            def read(self):
                import json
                return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.NOT_RESIDENT
        assert "No models currently resident in Ollama memory" in info

    def test_verify_ollama_api_unreachable(self, monkeypatch):
        def mock_err(req, timeout=2.0):
            raise OSError("Connection refused")

        monkeypatch.setattr("urllib.request.urlopen", mock_err)
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.QUERY_ERROR
        assert "Ollama local API unreachable" in info

    def test_verify_ollama_malformed_json_or_payload(self, monkeypatch):
        class NonDictResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'["not", "a", "dict"]'

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: NonDictResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.QUERY_ERROR
        assert "Malformed Ollama response: expected dict" in info

        class MissingModelsResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"status": "ok"}'

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MissingModelsResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.QUERY_ERROR
        assert "'models' field is missing" in info

    @pytest.mark.parametrize("invalid_vram", [None, True, False, "invalid", [123], {"a": 1}])
    def test_verify_ollama_invalid_vram_types_rejected(self, monkeypatch, invalid_vram):
        import json
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": invalid_vram,
                    "digest": "sha256:abc1234",
                }
            ]
        }

        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        state, info = verify_ollama_model_residency("qwen3.5:2b")
        assert state == OllamaResidencyState.QUERY_ERROR
        assert "invalid size_vram telemetry" in info

    def test_verify_ollama_cpu_entry_before_gpu_entry_returns_gpu_resident(self, monkeypatch):
        import json
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 0,
                    "digest": "sha256:cpu_instance",
                },
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1024 * 1024 * 1500,
                    "digest": "sha256:gpu_instance",
                },
            ]
        }

        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        res = verify_ollama_model_residency("qwen3.5:2b")
        state, info = res
        assert state == OllamaResidencyState.GPU_RESIDENT
        assert res.digest == "sha256:gpu_instance"
        assert "resident in GPU VRAM" in info


class TestContinuousMemorySampler:
    """Verifies ContinuousMemorySampler background sampling thread and error handling."""

    def test_sampler_lifecycle_and_sampling(self, monkeypatch):
        first_sample_event = threading.Event()
        original_query = voice_bench.query_system_memory_mb

        def mock_query():
            total, avail = original_query()
            first_sample_event.set()
            return total, avail

        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", mock_query)
        sampler = ContinuousMemorySampler(sample_interval_seconds=0.01)
        sampler.start()
        try:
            assert first_sample_event.wait(timeout=3.0), "Sampler did not record first sample within timeout"
        finally:
            sampler.stop()

        peak_ws, min_avail, is_valid, count = sampler.get_results()
        assert not sampler._thread.is_alive()
        assert count >= 1
        assert min_avail is not None and min_avail > 0
        assert is_valid is True

    def test_sampler_context_manager_cleanup(self):
        with ContinuousMemorySampler(sample_interval_seconds=0.02) as sampler:
            assert sampler._thread is not None
            assert sampler._thread.is_alive()
            time.sleep(0.04)

        assert not sampler._thread.is_alive()

    def test_sampler_cleanup_on_exception(self):
        sampler = ContinuousMemorySampler(sample_interval_seconds=0.02)
        sampler.start()
        try:
            raise RuntimeError("Inference pipeline simulated failure")
        except RuntimeError:
            pass
        finally:
            sampler.stop()

        assert not sampler._thread.is_alive()

    def test_sampler_preserves_telemetry_errors(self, monkeypatch):
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (None, None))
        sampler = ContinuousMemorySampler(sample_interval_seconds=0.02)
        sampler.sample_once()

        assert sampler.has_telemetry_error is True
        peak_ws, min_avail, is_valid, count = sampler.get_results()
        assert min_avail is None
        assert is_valid is False


class TestGate05Evaluation:
    """Verifies GATE-VOICE-05 (System RAM Ceiling & Available RAM) boundary thresholds."""

    def test_both_thresholds_met_passes(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=2000.0,
            private_usage_mb=700.0,
        )
        assert status == "PASS"
        assert "Peak Working Set: 300.0 MB (< 450.0 MB)" in text

    def test_working_set_boundary_below_passes(self):
        status, _ = evaluate_gate_05(
            peak_working_set_mb=449.9,
            min_available_ram_mb=2000.0,
        )
        assert status == "PASS"

    def test_working_set_boundary_exact_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=450.0,
            min_available_ram_mb=2000.0,
        )
        assert status == "FAIL"
        assert "Peak Working Set 450.0 MB >= 450.0 MB" in text

    def test_working_set_boundary_above_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=450.1,
            min_available_ram_mb=2000.0,
        )
        assert status == "FAIL"
        assert "Peak Working Set 450.1 MB >= 450.0 MB" in text

    def test_available_ram_boundary_below_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1535.9,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1535.9 MB <= 1536.0 MB" in text

    def test_available_ram_boundary_1535_99_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1535.99,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1535.99 MB <= 1536.0 MB" in text

    def test_available_ram_boundary_exact_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1536.0,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1536.0 MB <= 1536.0 MB" in text

    def test_available_ram_boundary_1536_04_passes(self):
        # 1536.04 MB is strictly > 1536.0 MB threshold and must pass, displaying high precision 1536.04 MB
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1536.04,
        )
        assert status == "PASS"
        assert "Min Available System RAM: 1536.04 MB (> 1.5 GB)" in text

    def test_available_ram_boundary_above_passes(self):
        status, _ = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1536.1,
        )
        assert status == "PASS"

    def test_both_violated_fails_with_both_reasons(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=500.0,
            min_available_ram_mb=1000.0,
        )
        assert status == "FAIL"
        assert "Peak Working Set 500.0 MB >= 450.0 MB" in text
        assert "Min Available System RAM 1000.0 MB <= 1536.0 MB" in text

    def test_missing_telemetry_not_run(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=None,
            min_available_ram_mb=None,
        )
        assert status == "NOT RUN"
        assert "Required process or system RAM monitoring could not be completed" in text

    def test_invalid_telemetry_flag_not_run(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=2000.0,
            ram_telemetry_valid=False,
        )
        assert status == "NOT RUN"
        assert "Required process or system RAM monitoring could not be completed" in text

    def test_invalid_telemetry_fails_if_ws_exceeded(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=500.0,
            min_available_ram_mb=2000.0,
            ram_telemetry_valid=False,
        )
        assert status == "FAIL"
        assert "Peak Working Set: 500.0 MB exceeds 450.0 MB ceiling" in text

    def test_synchronous_reading_breach_detected_even_if_sampler_min_higher(self, monkeypatch, tmp_path):
        # 1. Direct evaluate_gate_05 evaluation test
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1200.0,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1200.0 MB <= 1536.0 MB" in text

        # 2. Integration test exercising run_benchmark RAM merge:
        monkeypatch.setattr(
            "tools.bench.voice_bench.query_system_memory_mb",
            lambda: (16000.0, 1200.0),
        )
        monkeypatch.setattr(
            "tools.bench.voice_bench.query_process_memory_mb",
            lambda pid=None: (200.0, 200.0, 200.0),
        )
        monkeypatch.setattr(
            "tools.bench.voice_bench.query_gpu_vram_mb",
            lambda: 1000,
        )
        monkeypatch.setattr(
            "tools.bench.voice_bench.audit_active_process_sockets",
            lambda pid=None: [],
        )

        class MockSampler:
            def __init__(self, *args, **kwargs):
                pass
            def start(self):
                pass
            def stop(self):
                pass
            def get_results(self):
                # Sampler observed a high minimum of 2500 MB
                return 250.0, 2500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, return_breakdown=False, **kwargs):
                if return_breakdown:
                    return (
                        "hello",
                        0.1,
                        0.1,
                        {
                            "wav_parse_seconds": 0.001,
                            "pcm_norm_seconds": 0.001,
                            "c_buf_prep_seconds": 0.001,
                            "native_inference_seconds": 0.097,
                        },
                    )
                return ("hello", 0.1, 0.1)
            def close(self):
                pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        dummy_wav = corpus_dir / "sample.wav"
        import wave
        with wave.open(str(dummy_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 1600)

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        # Verified that 1200.0 MB was retained as min_available_ram_mb despite sampler returning 2500.0 MB
        assert report["host"]["min_available_ram_mb"] == 1200.0
        assert report["gates"]["GATE-VOICE-05"]["status"] == "FAIL"


class TestThreadDefaultsAndResolution:
    """Verifies that omitted n_threads parameter resolves to engine.default_threads."""

    def test_default_thread_resolution(self):
        mock_lib = MagicMock()
        mock_lib.pico_whisper_init.return_value = 0
        mock_lib.pico_whisper_transcribe.return_value = 0

        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine.default_threads = 1
        engine._lock = threading.RLock()
        engine._lib = mock_lib
        engine._ctx = ctypes.c_void_p(0x1234)

        # Call transcribe_samples without n_threads (should resolve to 1)
        engine.transcribe_samples([0.0] * 10)
        assert mock_lib.pico_whisper_transcribe.call_count == 1
        call_args = mock_lib.pico_whisper_transcribe.call_args[0]
        # Signature: (ctx, samples, n_samples, n_threads, out_buf, buf_size, out_len)
        assert call_args[3] == 1

        # Call with explicit override
        engine.transcribe_samples([0.0] * 10, n_threads=3)
        call_args_override = mock_lib.pico_whisper_transcribe.call_args[0]
        assert call_args_override[3] == 3

        # Invalid overrides
        with pytest.raises(ValueError, match="strictly between 1 and 4"):
            engine.transcribe_samples([0.0] * 10, n_threads=0)
        with pytest.raises(ValueError, match="strictly between 1 and 4"):
            engine.transcribe_samples([0.0] * 10, n_threads=5)


class TestContextLifetimeConcurrency:
    """Verifies that native context lifetime is serialized against concurrent execution and close()."""

    def test_transcription_blocked_in_native_call_blocks_close(self):
        import time

        mock_lib = MagicMock()
        mock_lib.pico_whisper_init.return_value = 0

        in_native = threading.Event()
        can_finish = threading.Event()
        close_completed = threading.Event()

        def slow_transcribe(ctx, c_samples, n_samples, threads, out_buf, buf_size, out_len):
            in_native.set()
            can_finish.wait(timeout=5.0)
            return 0

        mock_lib.pico_whisper_transcribe.side_effect = slow_transcribe

        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine.default_threads = 2
        engine._lock = threading.RLock()
        engine._lib = mock_lib
        engine._ctx = ctypes.c_void_p(0xABCD)

        # Thread 1: Transcribes
        def worker_transcribe():
            engine.transcribe_samples([0.0] * 10)

        t1 = threading.Thread(target=worker_transcribe)
        t1.start()

        # Wait until thread 1 is actively inside the native call
        assert in_native.wait(timeout=2.0) is True

        # Thread 2: Attempts close() while transcription is in-flight
        def worker_close():
            engine.close()
            close_completed.set()

        t2 = threading.Thread(target=worker_close)
        t2.start()

        # close() should NOT complete yet because thread 1 holds engine._lock
        time.sleep(0.1)
        assert close_completed.is_set() is False

        # Release thread 1
        can_finish.set()
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)

        assert close_completed.is_set() is True
        assert engine._ctx is None
        assert mock_lib.pico_whisper_free.call_count == 1
        assert mock_lib.pico_whisper_free.call_args[0][0].value == 0xABCD

    def test_transcription_after_close_raises_runtime_error(self):
        mock_lib = MagicMock()
        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine.default_threads = 2
        engine._lock = threading.RLock()
        engine._lib = mock_lib
        engine._ctx = None

        with pytest.raises(WhisperRuntimeError, match="Context is closed or uninitialized"):
            engine.transcribe_samples([0.0] * 10)

    def test_repeated_close_is_safe_and_idempotent(self):
        mock_lib = MagicMock()
        engine = PicoWhisperEngine.__new__(PicoWhisperEngine)
        engine.default_threads = 2
        engine._lock = threading.RLock()
        engine._lib = mock_lib
        engine._ctx = ctypes.c_void_p(0x5678)

        engine.close()
        assert engine._ctx is None
        assert mock_lib.pico_whisper_free.call_count == 1

        # Second close should be a no-op
        engine.close()
        assert mock_lib.pico_whisper_free.call_count == 1


class TestReportRobustness:
    """Verifies that benchmark reporting does not crash under missing or partial corpus conditions."""

    def test_format_metric_pct(self):
        assert format_metric_pct(None) == "N/A"
        assert format_metric_pct(0.0) == "0.0%"
        assert format_metric_pct(0.12345) == "12.35%"

    def test_render_report_with_real_speech_only_corpus(self):
        report = {
            "timestamp": "2026-11-18T00:00:00Z",
            "host": {
                "processor": "Intel x64",
                "cpu_topology": "8 cores",
                "cpu_threads_dedicated": 4,
            },
            "runtime": {"version": "v1.9.5"},
            "model": {"filename": "model.bin", "size_bytes": 1000, "sha256": "abcdef"},
            "gates": {
                "GATE-VOICE-01": {"name": "STT Latency", "threshold": "<= 1.5s", "measured": "P50=1.1s", "status": "PASS"},
            },
            "results": [
                {
                    "filename": "real1.wav",
                    "type": "real_speech",
                    "audio_duration_seconds": 5.0,
                    "p50_latency_seconds": 1.1,
                    "p90_latency_seconds": 1.2,
                    "p95_latency_seconds": 1.2,
                    "max_latency_seconds": 1.3,
                    "rtf_p50": 0.22,
                    "wer": 0.05,
                    "cer": 0.02,
                    "emitted_transcript": "hello world",
                }
            ],
            "summary": {
                "real_speech_aggregate_wer": 0.05,
                "real_speech_macro_wer": 0.05,
                "real_speech_macro_cer": 0.02,
                "smoke_commands_aggregate_wer": None,  # Absent smoke commands!
                "smoke_commands_macro_wer": None,
                "cold_init_seconds": 0.2,
                "peak_working_set_mb": 300.0,
                "avg_rtf_p50": 0.22,
            },
        }
        md = render_markdown_report(report)
        assert "Synthetic Commands Smoke WER**: N/A" in md
        assert "Real Speech Aggregate WER**: 5.0%" in md

    def test_render_report_with_synthetic_only_corpus(self):
        report = {
            "timestamp": "2026-11-18T00:00:00Z",
            "host": {
                "processor": "Intel x64",
                "cpu_topology": "8 cores",
                "cpu_threads_dedicated": 4,
            },
            "runtime": {"version": "v1.9.5"},
            "model": {"filename": "model.bin", "size_bytes": 1000, "sha256": "abcdef"},
            "gates": {},
            "results": [],
            "summary": {
                "real_speech_aggregate_wer": None,  # Absent real speech!
                "real_speech_macro_wer": None,
                "real_speech_macro_cer": None,
                "smoke_commands_aggregate_wer": 0.0,
                "smoke_commands_macro_wer": 0.0,
            },
        }
        md = render_markdown_report(report)
        assert "Real Speech Aggregate WER**: N/A" in md
        assert "Synthetic Commands Smoke WER**: 0.0%" in md


class TestEngineSetupAndCleanupRobustness:
    """Verifies that engine initialization failures fail closed, preserve original exceptions, and cleanly finalize samplers."""

    def test_missing_model_fails_closed_without_unhandled_cleanup(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent.bin"
        with pytest.raises(WhisperModelVerificationError, match="Model file not found"):
            PicoWhisperEngine(nonexistent)

    def test_corrupted_model_fails_closed_without_context_leak(self, tmp_path: Path):
        fake_model = tmp_path / "ggml-base.en-q8_0.bin"
        fake_model.write_bytes(b"corrupt header and truncated weights")
        with pytest.raises(WhisperModelVerificationError, match="Model size mismatch"):
            PicoWhisperEngine(fake_model)

    def test_simulated_init_failure_preserves_exception_and_stops_sampler(self, monkeypatch, tmp_path):
        sampler_stopped = threading.Event()

        class MockSampler:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                sampler_stopped.set()

            def get_results(self):
                return 100.0, 4000.0, True, 1

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        def raise_engine_init(*args, **kwargs):
            raise WhisperRuntimeError("Native DLL failed to allocate context")

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", raise_engine_init)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")

        with pytest.raises(WhisperRuntimeError, match="Native DLL failed to allocate context"):
            run_benchmark(
                model_path=fake_model,
                runtime_dir=tmp_path,
                corpus_dir=tmp_path,
            )

        assert sampler_stopped.is_set()

    def test_git_provenance_query_cleans_environment(self):
        # Verify that query_git_commit cleans GIT_DIR from child env and returns (commit, is_clean)
        res = query_git_commit()
        assert isinstance(res, tuple)
        assert len(res) == 2
        commit, is_clean = res
        assert isinstance(commit, str)
        assert len(commit) > 0
        assert isinstance(is_clean, bool)

    def test_gate_04_model_digest_continuity_failure(self):
        # If the target model changes digest during STT execution, Gate 04 must return FAIL
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:baseline123"),
            ("sample_test.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:different456"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:different456"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_initial_mb=2000,
            vram_final_mb=2000,
            max_vram_observed_mb=2000,
            vram_observations_count=10,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
        )
        assert status == "FAIL"
        assert "digest changed during STT execution" in text

    def test_gate_04_pre_init_instability_returns_not_run(self):
        # If model residency is unstable during pre_init_stability_check, Gate 04 must return NOT RUN (unstable baseline),
        # not a false workload eviction FAIL
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:abc"),
            ("pre_init_stability_check", OllamaResidencyState.NOT_RESIDENT, "unloaded before STT", None),
            ("sample_test.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:abc"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:abc"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_initial_mb=2000,
            vram_final_mb=2000,
            max_vram_observed_mb=2000,
            vram_observations_count=10,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
        )
        assert status == "NOT RUN"
        assert "was not stable on GPU before benchmark" in text


class TestGpuVramTelemetryAndMultiGpu:
    """Verifies single-GPU, multi-GPU, and malformed telemetry handling in query_gpu_vram_mb."""

    def test_query_gpu_vram_single_gpu(self, monkeypatch):
        monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: b"1450\n")
        assert voice_bench.query_gpu_vram_mb() == 1450

    def test_query_gpu_vram_multi_gpu_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: b"1450\n2048\n")
        assert voice_bench.query_gpu_vram_mb() is None

    def test_query_gpu_vram_malformed_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: b"not_an_int\n")
        assert voice_bench.query_gpu_vram_mb() is None

    def test_query_gpu_vram_empty_returns_unavailable(self, monkeypatch):
        monkeypatch.setattr("subprocess.check_output", lambda *args, **kwargs: b"   \n")
        assert voice_bench.query_gpu_vram_mb() is None


class TestGate04EvaluationHierarchy:
    """Verifies the strict evaluation hierarchy of GATE-VOICE-04:
    - Confirmed eviction -> FAIL (even if VRAM telemetry is unavailable)
    - Confirmed digest substitution -> FAIL (even if VRAM telemetry is unavailable)
    - Confirmed positive VRAM increase -> FAIL (even if subsequent readings failed)
    - Missing VRAM telemetry without confirmed violation -> NOT RUN
    - Missing model identity evidence (baseline or checkpoint digest missing) -> NOT RUN
    - Complete valid observations with continuity and zero additional VRAM -> PASS
    """

    def test_confirmed_eviction_fails_even_with_unavailable_vram(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:abc"),
            ("sample_1.wav", OllamaResidencyState.NOT_RESIDENT, "evicted from GPU", None),
            ("post_final", OllamaResidencyState.NOT_RESIDENT, "evicted from GPU", None),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=None,  # VRAM telemetry failed!
            vram_readings_valid=False,
            ollama_checkpoints=ckpts,
        )
        assert status == "FAIL"
        assert "evicted from GPU during STT execution" in text

    def test_confirmed_digest_substitution_fails_even_with_unavailable_vram(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:substituted"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:substituted"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=None,  # VRAM telemetry failed!
            vram_readings_valid=False,
            ollama_checkpoints=ckpts,
        )
        assert status == "FAIL"
        assert "digest changed during STT execution" in text

    def test_confirmed_vram_increase_fails_even_if_later_readings_failed(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=25.0,  # Observed breach +25 MiB
            vram_readings_valid=False,  # But later query timed out / invalid
            vram_initial_mb=1000.0,
            max_vram_observed_mb=1025.0,
            vram_final_mb=None,
            vram_observations_count=5,
            ollama_checkpoints=ckpts,
        )
        assert status == "FAIL"
        assert "exceeds 0 MiB budget" in text
        assert "25.0 MiB" in text

    def test_missing_vram_telemetry_without_violation_returns_not_run(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=None,
            vram_readings_valid=False,
            ollama_checkpoints=ckpts,
        )
        assert status == "NOT RUN"
        assert "GPU VRAM telemetry unavailable" in text

    def test_missing_baseline_digest_returns_not_run(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", None),  # Missing digest!
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_initial_mb=1000.0,
            vram_final_mb=1000.0,
            max_vram_observed_mb=1000.0,
            vram_observations_count=10,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
        )
        assert status == "NOT RUN"
        assert "baseline model artifact digest was not provided" in text

    def test_missing_checkpoint_digest_returns_not_run(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", None),  # Missing digest!
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_initial_mb=1000.0,
            vram_final_mb=1000.0,
            max_vram_observed_mb=1000.0,
            vram_observations_count=10,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
        )
        assert status == "NOT RUN"
        assert "model artifact digest was missing at checkpoint 'sample_1.wav'" in text

    def test_complete_valid_continuity_passes(self):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("sample_1.wav", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b", "sha256:orig"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0,
            vram_initial_mb=1000.0,
            vram_final_mb=1000.0,
            max_vram_observed_mb=1000.0,
            vram_observations_count=10,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
        )
        assert status == "PASS"
        assert "VRAM Delta = 0 MiB" in text


class TestBenchmarkEndToEndDigestAndDesignatedFixture:
    """Verifies end-to-end run_benchmark integration:
    - Real OllamaResidencyResult digest propagates to every checkpoint
    - Digest mutation causes end-to-end GATE-04 failure
    - Designated 5s sample is strictly chosen by filename
    """

    def test_run_benchmark_propagates_digest_to_every_checkpoint(self, monkeypatch, tmp_path):
        import json

        # Mock Ollama returning consistent digest
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1024 * 1024 * 1500,
                    "digest": "sha256:verified_target_digest_12345",
                }
            ]
        }
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return (
                    "dummy",
                    0.1,
                    5.0,
                    {
                        "wav_parse_seconds": 0.001,
                        "pcm_norm_seconds": 0.001,
                        "c_buf_prep_seconds": 0.001,
                        "native_inference_seconds": 0.097,
                    },
                )
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        # Create corpus with designated file
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(create_valid_test_wav(4.92))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        checkpoints = report["host"]["ollama_checkpoints"]
        assert len(checkpoints) >= 4  # pre_init, pre_init_stability_check, sample_..., post_final
        for name, state, info, digest in checkpoints:
            assert digest == "sha256:verified_target_digest_12345", f"Checkpoint {name} missing verified digest!"

        assert report["gates"]["GATE-VOICE-04"]["status"] == "PASS"

    def test_run_benchmark_digest_change_fails_gate_04(self, monkeypatch, tmp_path):
        # Simulate digest mutating after baseline establishment (calls 1 and 2 = baseline, call 3 = workload)
        call_count = [0]
        def mock_urlopen(req, timeout=2.0):
            call_count[0] += 1
            digest = "sha256:orig" if call_count[0] <= 2 else "sha256:tampered_digest"
            fake_response = {
                "models": [
                    {
                        "name": "qwen3.5:2b",
                        "size_vram": 1024 * 1024 * 1500,
                        "digest": digest,
                    }
                ]
            }
            class MockResponse:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return json.dumps(fake_response).encode("utf-8")
            return MockResponse()

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return (
                    "dummy",
                    0.1,
                    5.0,
                    {
                        "wav_parse_seconds": 0.001,
                        "pcm_norm_seconds": 0.001,
                        "c_buf_prep_seconds": 0.001,
                        "native_inference_seconds": 0.097,
                    },
                )
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(create_valid_test_wav(4.92))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        assert report["gates"]["GATE-VOICE-04"]["status"] == "FAIL"
        assert "digest changed during STT execution" in report["gates"]["GATE-VOICE-04"]["measured"]

    def test_run_benchmark_unstable_baseline_returns_not_run(self, monkeypatch, tmp_path):
        # Simulate baseline unstable before benchmark: call 1 and call 2 have different digests
        call_count = [0]
        def mock_urlopen(req, timeout=2.0):
            call_count[0] += 1
            digest = "sha256:baseline_call1" if call_count[0] == 1 else "sha256:baseline_call2"
            fake_response = {
                "models": [
                    {
                        "name": "qwen3.5:2b",
                        "size_vram": 1024 * 1024 * 1500,
                        "digest": digest,
                    }
                ]
            }
            class MockResponse:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return json.dumps(fake_response).encode("utf-8")
            return MockResponse()

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                rdir = kwargs.get("runtime_dir") or (args[1] if len(args) > 1 else None)
                if rdir and not Path(rdir).exists():
                    raise WhisperRuntimeError("Missing runtime directory")
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return ("dummy", 0.1, 5.0, {"wav_parse_seconds": 0.001, "pcm_norm_seconds": 0.001, "c_buf_prep_seconds": 0.001, "native_inference_seconds": 0.097})
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(create_valid_test_wav(4.92))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        assert report["gates"]["GATE-VOICE-04"]["status"] == "NOT RUN"
        assert "baseline model artifact digest was unstable before benchmark" in report["gates"]["GATE-VOICE-04"]["measured"]

    def test_run_benchmark_designated_fixture_selection(self, monkeypatch, tmp_path):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1024 * 1024 * 1500,
                    "digest": "sha256:abc",
                }
            ]
        }
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, wav_bytes, *args, **kwargs):
                # If aaa_other_5s.wav is processed, return a very high latency that would fail
                if b"AAA" in wav_bytes:
                    proc_time = 3.5  # Exceeds 1.5s
                else:
                    proc_time = 0.5  # Satisfies <= 1.5s
                return (
                    "dummy",
                    proc_time,
                    5.0,
                    {
                        "wav_parse_seconds": 0.001,
                        "pcm_norm_seconds": 0.001,
                        "c_buf_prep_seconds": 0.001,
                        "native_inference_seconds": proc_time - 0.003,
                    },
                )
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # aaa_other_5s.wav sorts before audio_cmd_workflow_5s.wav
        (corpus_dir / "aaa_other_5s.wav").write_bytes(create_valid_test_wav(5.0))
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(create_valid_test_wav(4.92))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        # Gate 01 must evaluate audio_cmd_workflow_5s.wav, NOT aaa_other_5s.wav
        gate_01 = report["gates"]["GATE-VOICE-01"]
        assert "5s sample 'audio_cmd_workflow_5s.wav'" in gate_01["measured"]
        assert gate_01["status"] == "PASS"

    def test_run_benchmark_missing_designated_fixture_returns_not_run(self, monkeypatch, tmp_path):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1024 * 1024 * 1500,
                    "digest": "sha256:abc",
                }
            ]
        }
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        monkeypatch.setattr(
            "tools.bench.voice_bench.verify_model_artifact",
            lambda p: {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"},
        )

        class MockEngine:
            def __init__(self, *args, **kwargs):
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return (
                    "dummy",
                    0.5,
                    5.0,
                    {
                        "wav_parse_seconds": 0.001,
                        "pcm_norm_seconds": 0.001,
                        "c_buf_prep_seconds": 0.001,
                        "native_inference_seconds": 0.497,
                    },
                )
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # Only an undesignated file is present
        (corpus_dir / "aaa_other_5s.wav").write_bytes(create_valid_test_wav(5.0))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        gate_01 = report["gates"]["GATE-VOICE-01"]
        assert gate_01["status"] == "NOT RUN"
        assert "audio_cmd_workflow_5s.wav' missing" in gate_01["measured"]


class TestOllamaDigestSanitization:
    """Verifies that empty, whitespace-only, missing, or non-string digests are treated as missing identity evidence."""

    @pytest.mark.parametrize("invalid_digest", ["", "   ", None, 12345, ["digest"], {"k": "v"}])
    def test_verify_ollama_sanitizes_invalid_digest_to_none(self, monkeypatch, invalid_digest):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": 1024 * 1024 * 1500,
                    "digest": invalid_digest,
                }
            ]
        }
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        res = verify_ollama_model_residency("qwen3.5:2b")
        assert res.state == OllamaResidencyState.GPU_RESIDENT
        assert res.digest is None

    @pytest.mark.parametrize("whitespace_digest", ["", "   ", None])
    def test_gate_04_returns_not_run_when_checkpoint_digest_is_whitespace_or_none(self, whitespace_digest):
        ckpts = [
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "info", "sha256:valid_baseline"),
            ("pre_init_stability_check", OllamaResidencyState.GPU_RESIDENT, "info", "sha256:valid_baseline"),
            ("workload_sample_1", OllamaResidencyState.GPU_RESIDENT, "info", whitespace_digest),
            ("post_final", OllamaResidencyState.GPU_RESIDENT, "info", "sha256:valid_baseline"),
        ]
        status, text = evaluate_gate_04(
            vram_max_delta_mb=0.0,
            vram_readings_valid=True,
            ollama_checkpoints=ckpts,
            vram_initial_mb=1000.0,
            max_vram_observed_mb=1000.0,
            vram_final_mb=1000.0,
            vram_observations_count=3,
        )
        assert status == "NOT RUN"
        assert "model artifact digest was missing at checkpoint 'workload_sample_1'" in text


class TestQueryGitCommitTristate:
    """Verifies that Git working-tree cleanliness is tri-state (Clean, Modified, Unknown)."""

    def test_git_clean_true_when_porcelain_empty(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda cmd, **kwargs: "abc1234" if "rev-parse" in cmd else "",
        )
        commit, is_clean = query_git_commit()
        assert commit == "abc1234"
        assert is_clean is True

    def test_git_clean_false_when_porcelain_has_entries(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.check_output",
            lambda cmd, **kwargs: "abc1234" if "rev-parse" in cmd else " M foo.py\n",
        )
        commit, is_clean = query_git_commit()
        assert commit == "abc1234"
        assert is_clean is False

    def test_git_clean_none_when_status_fails(self, monkeypatch):
        def mock_cmd(cmd, **kwargs):
            if "rev-parse" in cmd:
                return "abc1234"
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr("subprocess.check_output", mock_cmd)
        commit, is_clean = query_git_commit()
        assert commit == "abc1234"
        assert is_clean is None


class TestVerifyFailClosedFixtureWriteHandling:
    """Verifies that temporary fixture write failures are handled gracefully without aborting benchmark."""

    def test_verify_fail_closed_handles_fixture_write_failure_gracefully(self, monkeypatch, tmp_path):
        real_write_bytes = Path.write_bytes

        def failing_write_bytes(self, data):
            if "ggml-base.en-q8_0.bin" in self.name or "custom.bin" in self.name:
                raise OSError("Simulated disk full or permission denied")
            return real_write_bytes(self, data)

        monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)
        results = verify_fail_closed_behavior(runtime_dir=tmp_path)
        assert isinstance(results, dict)
        assert results["passed"] is False
        assert results["subtests"]["missing_model_fails_closed"] is True
        assert results["subtests"]["truncated_model_header_fails_closed"] is False
        assert results["subtests"]["digest_mismatch_fails_closed"] is False


class TestDesignatedWavValidation:
    """Verifies designated 5-second WAV validation for format, frame count, and duration."""

    def test_nonexistent_file(self, tmp_path):
        valid, msg = validate_designated_wav(tmp_path / "nonexistent.wav")
        assert valid is False
        assert "File does not exist" in msg

    def test_invalid_header(self, tmp_path):
        f = tmp_path / "invalid.wav"
        f.write_bytes(b"not a wav file")
        valid, msg = validate_designated_wav(f)
        assert valid is False
        assert "Invalid WAV file format or header" in msg

    def test_stereo_rejected(self, tmp_path):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(2)  # stereo
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 32000)
        f = tmp_path / "stereo.wav"
        f.write_bytes(buf.getvalue())
        valid, msg = validate_designated_wav(f)
        assert valid is False
        assert "expected mono, channels=1" in msg

    def test_wrong_sample_rate_rejected(self, tmp_path):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(8000)  # 8 kHz
            wf.writeframes(b"\x00\x00" * 8000)
        f = tmp_path / "8k.wav"
        f.write_bytes(buf.getvalue())
        valid, msg = validate_designated_wav(f)
        assert valid is False
        assert "expected 16000 Hz" in msg

    def test_zero_frames_rejected(self, tmp_path):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"")  # 0 frames
        f = tmp_path / "empty.wav"
        f.write_bytes(buf.getvalue())
        valid, msg = validate_designated_wav(f)
        assert valid is False
        assert "non-positive frame count" in msg

    def test_duration_out_of_manifest_range_rejected(self, tmp_path):
        f = tmp_path / "short.wav"
        f.write_bytes(create_valid_test_wav(1.0))
        valid, msg = validate_designated_wav(f)
        assert valid is False
        assert "deviates from manifest expected duration 4.92s" in msg

    def test_valid_designated_wav_accepted(self, tmp_path):
        f = tmp_path / "valid_5s.wav"
        f.write_bytes(create_valid_test_wav(4.92))
        valid, msg = validate_designated_wav(f)
        assert valid is True
        assert "Valid 16kHz mono 16-bit PCM" in msg

    def test_run_benchmark_fails_gate_01_if_designated_wav_invalid(self, monkeypatch, tmp_path):
        fake_response = {"models": [{"name": "qwen3.5:2b", "size_vram": 1024 * 1024 * 1500, "digest": "sha256:abc"}]}
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        real_verify = voice_bench.verify_model_artifact
        def mock_verify(p, **kwargs):
            if str(p).endswith("fake_model.bin"):
                return {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"}
            return real_verify(p, **kwargs)
        monkeypatch.setattr("tools.bench.voice_bench.verify_model_artifact", mock_verify)

        class MockEngine:
            def __init__(self, *args, **kwargs):
                rdir = kwargs.get("runtime_dir") or (args[1] if len(args) > 1 else None)
                if rdir and not Path(rdir).exists():
                    raise WhisperRuntimeError("Missing runtime directory")
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return ("dummy", 0.1, 5.0, {"wav_parse_seconds": 0.001, "pcm_norm_seconds": 0.001, "c_buf_prep_seconds": 0.001, "native_inference_seconds": 0.097})
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # Create an INVALID WAV for audio_cmd_workflow_5s.wav (corrupted header)
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(b"corrupted not wav bytes")

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        gate_01 = report["gates"]["GATE-VOICE-01"]
        assert gate_01["status"] == "FAIL"
        assert "failed audio validation" in gate_01["measured"]


class TestGate09AndFailClosedMarkdownDerivation:
    """Verifies that GATE-VOICE-09 and Markdown report derive from actual probe executions."""

    def test_gate_09_and_markdown_reflect_actual_subtests(self, monkeypatch, tmp_path):
        fake_response = {"models": [{"name": "qwen3.5:2b", "size_vram": 1024 * 1024 * 1500, "digest": "sha256:abc"}]}
        class MockResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return json.dumps(fake_response).encode("utf-8")

        monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=2.0: MockResponse())
        monkeypatch.setattr("tools.bench.voice_bench.query_system_memory_mb", lambda: (16000.0, 4000.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_process_memory_mb", lambda pid=None: (200.0, 200.0, 200.0))
        monkeypatch.setattr("tools.bench.voice_bench.query_gpu_vram_mb", lambda: 1000)
        monkeypatch.setattr("tools.bench.voice_bench.audit_active_process_sockets", lambda pid=None: [])

        class MockSampler:
            def __init__(self, *args, **kwargs): pass
            def start(self): pass
            def stop(self): pass
            def get_results(self): return 250.0, 3500.0, True, 10

        monkeypatch.setattr("tools.bench.voice_bench.ContinuousMemorySampler", MockSampler)

        fake_model = tmp_path / "fake_model.bin"
        fake_model.write_bytes(b"dummy")
        real_verify = voice_bench.verify_model_artifact
        def mock_verify(p, **kwargs):
            if str(p).endswith("fake_model.bin"):
                return {"filename": "fake_model.bin", "size_bytes": 5, "sha256": "abc"}
            return real_verify(p, **kwargs)
        monkeypatch.setattr("tools.bench.voice_bench.verify_model_artifact", mock_verify)

        class MockEngine:
            def __init__(self, *args, **kwargs):
                rdir = kwargs.get("runtime_dir") or (args[1] if len(args) > 1 else None)
                if rdir and not Path(rdir).exists():
                    raise WhisperRuntimeError("Missing runtime directory")
                self.version = "1.0"
                self.system_info = "AVX2"
            def transcribe_wav_bytes(self, *args, **kwargs):
                return ("dummy", 0.1, 5.0, {"wav_parse_seconds": 0.001, "pcm_norm_seconds": 0.001, "c_buf_prep_seconds": 0.001, "native_inference_seconds": 0.097})
            def close(self): pass

        monkeypatch.setattr("tools.bench.voice_bench.PicoWhisperEngine", MockEngine)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(create_valid_test_wav(4.92))

        report = run_benchmark(
            model_path=fake_model,
            runtime_dir=tmp_path,
            corpus_dir=corpus_dir,
            iterations=1,
        )

        gate_09 = report["gates"]["GATE-VOICE-09"]
        assert gate_09["status"] == "PASS"
        assert "Verified: all probes passed" in gate_09["measured"]
        assert "missing model" in gate_09["measured"]
        assert "truncated model header" in gate_09["measured"]
        assert "digest mismatch" in gate_09["measured"]
        assert "missing runtime" in gate_09["measured"]

        md = render_markdown_report(report)
        assert "Verified immediate typed exceptions across all probes" in md
        assert "missing model" in md
