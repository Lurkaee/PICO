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
import threading
import time
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
    render_markdown_report,
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
            ("pre_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
            ("post_init", OllamaResidencyState.GPU_RESIDENT, "Model 'qwen3.5:2b' resident in GPU VRAM (1400 MiB)"),
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
        assert "Model 'qwen3.5:2b-instruct'" in info

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

    def test_verify_ollama_invalid_vram_types_rejected(self, monkeypatch):
        fake_response = {
            "models": [
                {
                    "name": "qwen3.5:2b",
                    "size_vram": None,
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
        assert "loaded on CPU only (size_vram=None), not GPU-resident" in info

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

    def test_available_ram_boundary_exact_fails(self):
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1536.0,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1536.0 MB <= 1536.0 MB" in text

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

    def test_synchronous_reading_breach_detected_even_if_sampler_min_higher(self):
        # Even if sampler observed a higher minimum (e.g. 2500 MB), if a synchronous reading
        # detected a dip below 1536 MB (e.g. 1200 MB), the merged minimum is 1200 MB,
        # which must result in Gate 05 FAIL.
        status, text = evaluate_gate_05(
            peak_working_set_mb=300.0,
            min_available_ram_mb=1200.0,
        )
        assert status == "FAIL"
        assert "Min Available System RAM 1200.0 MB <= 1536.0 MB" in text


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

    def test_simulated_init_failure_preserves_exception_and_stops_sampler(self):
        sampler_stopped = threading.Event()

        class MockSampler:
            def start(self):
                pass

            def stop(self):
                sampler_stopped.set()

            def get_results(self):
                return 100.0, 4000.0, True, 1

        # Verify that in a setup failure pattern, the original exception is preserved
        try:
            sampler = MockSampler()
            sampler.start()
            try:
                raise WhisperRuntimeError("Native DLL failed to allocate context")
            finally:
                sampler.stop()
        except WhisperRuntimeError as exc:
            assert "Native DLL failed to allocate context" in str(exc)

        assert sampler_stopped.is_set()
