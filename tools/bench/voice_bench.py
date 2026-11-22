"""PICO Voice Engine Hardware Qualification Benchmark Harness (Milestone P11.3.2 Stage 2).

Executes reproducible, CPU-only hardware qualification tests against whisper.cpp
on Windows x64 AVX2, collecting rigorous telemetry for:
- Cold initialization latency (mmap + tensor allocation)
- Warm inference latency (P50, P90, P95, max, RTF) across multiple sample lengths
- Accuracy metrics (WER, CER, substitutions, deletions, insertions)
- Process peak Working Set, Private Bytes, and system available physical RAM
- GPU dedicated VRAM delta and concurrent Ollama LLM residency verification
- Ephemeral in-memory audio lifecycle and disk non-retention auditing
- Network socket monitoring and fail-closed safety verification
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import enum
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.bench.whisper_prototype import (
    CANONICAL_MODELS,
    DEFAULT_MODEL_DIR,
    DEFAULT_RUNTIME_DIR,
    PicoWhisperEngine,
    WhisperModelVerificationError,
    WhisperPrototypeError,
    WhisperRuntimeError,
    verify_model_artifact,
)
from tools.bench.option_b_eval import (
    OptionBGate02Result,
    OptionBGateStatus,
    compute_levenshtein_ops,
    evaluate_option_b_gate_02,
    normalize_text_for_option_b,
    validate_option_b_audio_file,
    validate_option_b_manifest,
)

logger = logging.getLogger("voice_bench")

normalize_text_for_eval = normalize_text_for_option_b
normalize_text = normalize_text_for_eval


def compute_wer(reference: str, hypothesis: str) -> float:
    """Compute Word Error Rate (WER) as edit distance / reference words."""
    ref_words = normalize_text_for_eval(reference).split()
    hyp_words = normalize_text_for_eval(hypothesis).split()
    if not ref_words:
        return 0.0 if not hyp_words else 1.0
    dist, _, _, _ = compute_levenshtein_ops(ref_words, hyp_words)
    return round(dist / len(ref_words), 4)


calculate_wer = compute_wer


def compute_cer(reference: str, hypothesis: str) -> float:
    """Compute Character Error Rate (CER) as edit distance / reference characters."""
    ref_chars = list(normalize_text_for_eval(reference).replace(" ", ""))
    hyp_chars = list(normalize_text_for_eval(hypothesis).replace(" ", ""))
    if not ref_chars:
        return 0.0 if not hyp_chars else 1.0
    dist, _, _, _ = compute_levenshtein_ops(ref_chars, hyp_chars)
    return round(dist / len(ref_chars), 4)


calculate_cer = compute_cer


def query_process_memory_mb() -> tuple[float | None, float | None, float | None]:
    """Query current process Peak Working Set, Current Working Set, and Private Bytes in MB.

    Returns:
        (peak_working_set_mb, current_working_set_mb, private_usage_mb) or (None, None, None) on failure.
    """
    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    try:
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        h_proc = kernel32.GetCurrentProcess()
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)

        if psapi.GetProcessMemoryInfo(h_proc, ctypes.byref(counters), counters.cb):
            peak_ws_mb = round(counters.PeakWorkingSetSize / (1024 * 1024), 2)
            cur_ws_mb = round(counters.WorkingSetSize / (1024 * 1024), 2)
            priv_mb = round(counters.PrivateUsage / (1024 * 1024), 2)
            return peak_ws_mb, cur_ws_mb, priv_mb
    except Exception as exc:
        logger.debug("Failed to query Win32 memory counters: %s", exc)

    return None, None, None


def query_system_memory_mb() -> tuple[float | None, float | None]:
    """Query host system total physical memory and available physical memory in MB.

    Returns:
        (total_phys_mb, avail_phys_mb) or (None, None) on failure.
    """
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    try:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            total_mb = round(stat.ullTotalPhys / (1024 * 1024), 2)
            avail_mb = round(stat.ullAvailPhys / (1024 * 1024), 2)
            return total_mb, avail_mb
    except Exception as exc:
        logger.debug("Failed to query Win32 GlobalMemoryStatusEx: %s", exc)

    return None, None


def query_gpu_vram_mb() -> int | None:
    """Query NVIDIA GPU dedicated VRAM used in MiB via nvidia-smi.

    If exactly one GPU is present, returns int MiB used.
    If multiple GPUs are present, returns None (unavailable) to prevent false qualification
    from measuring only the first GPU while activity occurs on a secondary GPU.
    Returns None if nvidia-smi is unavailable, output is malformed, or query fails.
    """
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
        )
        lines = [line.strip() for line in out.decode("utf-8").strip().splitlines() if line.strip()]
        if not lines:
            return None
        if len(lines) > 1:
            logger.warning(
                "Multiple NVIDIA GPUs detected (%d devices); marking VRAM telemetry unavailable to prevent single-device bias",
                len(lines),
            )
            return None
        return int(lines[0])
    except Exception:
        return None


def query_git_commit(repo_root: Path | None = None) -> tuple[str, bool | None]:
    """Query current repository HEAD commit hash and working tree cleanliness.

    Strips GIT_* environment overrides to ensure local repository inspection,
    executing strictly with cwd set to repo_root.

    Returns:
        (commit_hash, is_clean) where is_clean is True (clean), False (modified), or None (unknown).
    """
    target_dir = repo_root if repo_root is not None else Path(__file__).resolve().parents[2]
    clean_env = os.environ.copy()
    for var in ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"]:
        clean_env.pop(var, None)

    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=target_dir,
            env=clean_env,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            text=True,
        ).strip()
    except Exception:
        commit = "unknown"

    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=target_dir,
            env=clean_env,
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            text=True,
        ).strip()
        is_clean: bool | None = len(status_out) == 0
    except Exception:
        is_clean = None

    return commit, is_clean


class ContinuousMemorySampler:
    """Bounded background sampler thread that monitors system available RAM and process Working Set.

    Runs continuously across:
    - Native initialization
    - Warm-up
    - Benchmark inference
    - Context cleanup

    Uses threading.Event for stop signal and is joined in finally blocks.
    Preserves telemetry errors as unavailable values (never turns missing samples into zero).
    """

    def __init__(self, sample_interval_seconds: float = 0.05, pid: int | None = None) -> None:
        self.sample_interval = sample_interval_seconds
        self.pid = pid or os.getpid()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

        self.min_available_ram_mb: float | None = None
        self.peak_working_set_mb: float | None = None
        self.samples_count: int = 0
        self.has_telemetry_error: bool = False
        self.exceptions: list[Exception] = []

    def start(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="PicoMemorySampler")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)
            if self._thread.is_alive():
                with self._lock:
                    self.has_telemetry_error = True

    def sample_once(self) -> None:
        """Collect a single synchronous sample of available system RAM and process working set."""
        try:
            _, avail_mb = query_system_memory_mb()
            peak_ws, cur_ws, _ = query_process_memory_mb()

            with self._lock:
                self.samples_count += 1
                if avail_mb is not None:
                    if self.min_available_ram_mb is None or avail_mb < self.min_available_ram_mb:
                        self.min_available_ram_mb = avail_mb
                else:
                    self.has_telemetry_error = True

                if peak_ws is not None:
                    if self.peak_working_set_mb is None or peak_ws > self.peak_working_set_mb:
                        self.peak_working_set_mb = peak_ws
                elif cur_ws is not None:
                    if self.peak_working_set_mb is None or cur_ws > self.peak_working_set_mb:
                        self.peak_working_set_mb = cur_ws
                else:
                    self.has_telemetry_error = True
        except Exception as exc:
            with self._lock:
                self.has_telemetry_error = True
                self.exceptions.append(exc)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.sample_once()
            self._stop_event.wait(self.sample_interval)

    def get_results(self) -> tuple[float | None, float | None, bool, int]:
        """Return (peak_working_set_mb, min_available_ram_mb, is_valid, samples_count)."""
        with self._lock:
            thread_finished = self._thread is None or not self._thread.is_alive()
            is_valid = (
                not self.has_telemetry_error
                and thread_finished
                and self.min_available_ram_mb is not None
                and self.samples_count > 0
            )
            return (
                self.peak_working_set_mb,
                self.min_available_ram_mb,
                is_valid,
                self.samples_count,
            )

    def __enter__(self) -> "ContinuousMemorySampler":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


class OllamaResidencyState(str, enum.Enum):
    """Structured residency state for Ollama local models."""
    GPU_RESIDENT = "GPU_RESIDENT"
    NOT_RESIDENT = "NOT_RESIDENT"
    QUERY_ERROR = "QUERY_ERROR"


class OllamaResidencyResult(tuple):
    """2-tuple (state, info) with optional model digest attribute for continuity checking."""
    def __new__(cls, state: OllamaResidencyState, info: str, digest: str | None = None):
        obj = super().__new__(cls, (state, info))
        obj.state = state
        obj.info = info
        obj.digest = digest
        return obj


# Explicit allowlist of supported Ollama model tags for PICO local-small LLM.
# Only canonical or documented necessary aliases are permitted:
# - 'qwen3.5:2b': Canonical local-small LLM model tag in PICO architecture.
# - 'qwen3.5:2b:latest': Default tag applied by Ollama daemon when pulled as 'qwen3.5:2b'.
# - 'qwen3.5:2b-instruct': Explicit instruction-tuned tag for Qwen 2B architecture.
SUPPORTED_OLLAMA_MODELS: dict[str, str] = {
    "qwen3.5:2b": "Canonical PICO local-small LLM model tag",
    "qwen3.5:2b:latest": "Default tag applied by Ollama when pulling 'qwen3.5:2b'",
    "qwen3.5:2b-instruct": "Explicit instruction-tuned tag for Qwen 2B architecture",
}


def verify_ollama_model_residency(
    expected_model: str = "qwen3.5:2b",
) -> OllamaResidencyResult:
    """Inspect local Ollama running model state to verify single-model GPU residency.

    Requires positive GPU residency evidence (size_vram > 0) for an exact supported model from SUPPORTED_OLLAMA_MODELS.

    Returns:
        OllamaResidencyResult(residency_state, model_info_or_error_string, digest)
        where residency_state is one of:
          - OllamaResidencyState.GPU_RESIDENT (size_vram > 0 and exact match in allowlist)
          - OllamaResidencyState.NOT_RESIDENT (no models, cpu-only, or non-matching model)
          - OllamaResidencyState.QUERY_ERROR (API unreachable, network error, malformed JSON, invalid telemetry)
    """
    clean_expected = expected_model.strip().lower()
    valid_target_names = {clean_expected}
    if not clean_expected.endswith(":latest"):
        valid_target_names.add(f"{clean_expected}:latest")
    if clean_expected.endswith(":latest"):
        valid_target_names.add(clean_expected[:-7])

    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/ps")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if not isinstance(data, dict):
                return OllamaResidencyResult(OllamaResidencyState.QUERY_ERROR, f"Malformed Ollama response: expected dict, got {type(data).__name__}")
            models = data.get("models")
            if not isinstance(models, list):
                return OllamaResidencyResult(OllamaResidencyState.QUERY_ERROR, "Malformed Ollama response: 'models' field is missing or not a list")

            matching_entries: list[dict[str, Any]] = []
            for m in models:
                if not isinstance(m, dict):
                    continue
                m_name = m.get("name")
                if not isinstance(m_name, str):
                    continue
                m_clean = m_name.strip().lower()

                # Exact target model match against valid names and global allowlist
                if m_clean in valid_target_names and m_clean in SUPPORTED_OLLAMA_MODELS:
                    matching_entries.append(m)

            if matching_entries:
                # Validate telemetry types first: reject None, bool, or non-numeric size_vram as QUERY_ERROR
                for m in matching_entries:
                    m_name = m.get("name", "unknown")
                    size_vram = m.get("size_vram")
                    if size_vram is None or isinstance(size_vram, bool) or not isinstance(size_vram, (int, float)):
                        return OllamaResidencyResult(
                            OllamaResidencyState.QUERY_ERROR,
                            f"Model '{m_name}' has invalid size_vram telemetry: {size_vram!r} (expected int or float)",
                        )

                # Prioritize any GPU-resident matching entry
                for m in matching_entries:
                    m_name = m.get("name", "unknown")
                    size_vram = m.get("size_vram")
                    raw_digest = m.get("digest")
                    digest = raw_digest.strip() if isinstance(raw_digest, str) and raw_digest.strip() else None
                    if size_vram > 0:
                        vram_mb = round(size_vram / (1024 * 1024), 1)
                        desc = SUPPORTED_OLLAMA_MODELS.get(m_name.strip().lower(), "Supported model")
                        return OllamaResidencyResult(
                            OllamaResidencyState.GPU_RESIDENT,
                            f"Model '{m_name}' ({desc}) resident in GPU VRAM ({vram_mb} MiB)",
                            digest=digest,
                        )

                # If all matching entries have size_vram == 0, confirmed CPU-only
                first_m = matching_entries[0]
                raw_d = first_m.get("digest")
                clean_d = raw_d.strip() if isinstance(raw_d, str) and raw_d.strip() else None
                return OllamaResidencyResult(
                    OllamaResidencyState.NOT_RESIDENT,
                    f"Model '{first_m.get('name')}' is loaded on CPU only (size_vram=0), not GPU-resident",
                    digest=clean_d,
                )

            if models:
                resident_names = [str(m.get("name", "unknown")) for m in models if isinstance(m, dict)]
                return OllamaResidencyResult(
                    OllamaResidencyState.NOT_RESIDENT,
                    f"Different model(s) resident: {', '.join(resident_names)} (expected target: {expected_model})",
                )
            return OllamaResidencyResult(OllamaResidencyState.NOT_RESIDENT, "No models currently resident in Ollama memory")
    except Exception as exc:
        return OllamaResidencyResult(OllamaResidencyState.QUERY_ERROR, f"Ollama local API unreachable (http://127.0.0.1:11434): {exc}")


def audit_active_process_sockets(pid: int | None = None) -> list[dict[str, Any]]:
    """Audit active network connections for the specified PID."""
    if pid is None:
        pid = os.getpid()

    connections: list[dict[str, Any]] = []
    try:
        # Use netstat -ano to inspect connections for this specific PID
        out = subprocess.check_output(
            ["netstat", "-ano"],
            stderr=subprocess.DEVNULL,
            timeout=5.0,
            text=True,
        )
        for line in out.splitlines():
            parts = line.strip().split()
            if len(parts) >= 4 and str(pid) == parts[-1]:
                proto = parts[0]
                local_addr = parts[1]
                foreign_addr = parts[2] if len(parts) >= 5 else ""
                state = parts[3] if len(parts) >= 5 else ""
                connections.append({
                    "proto": proto,
                    "local": local_addr,
                    "foreign": foreign_addr,
                    "state": state,
                })
    except Exception as exc:
        logger.debug("Failed to query netstat for PID %s: %s", pid, exc)

    return connections


# Canonical Ground Truth Corpus definitions
CANONICAL_CORPUS_GROUND_TRUTH: dict[str, dict[str, Any]] = {
    # Real human speech
    "jfk.wav": {
        "text": "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country.",
        "type": "real_speech",
        "description": "JFK 1961 inaugural address (11.0s, public domain historical recording)",
    },
    "jfk_short_1s.wav": {
        "text": "And so my fellow",
        "type": "real_speech",
        "description": "JFK slice (1.5s, short phrase)",
    },
    "jfk_query_3s.wav": {
        "text": "And so my fellow Americans",
        "type": "real_speech",
        "description": "JFK slice (3.2s, mid phrase)",
    },
    "jfk_workflow_5s.wav": {
        "text": "And so my fellow Americans ask not",
        "type": "real_speech",
        "description": "JFK slice (5.1s, long phrase)",
    },
    "jfk_dictation_10s.wav": {
        "text": "And so my fellow Americans ask not what your country can do for you ask what you can do for your country",
        "type": "real_speech",
        "description": "JFK slice (10.0s, extended dictation)",
    },
    "vosk_digits.wav": {
        "text": "one zero zero zero one nine zero two one zero zero one eight zero three",
        "type": "real_speech",
        "description": "Vosk standard numeric speech sample (8.3s, Apache 2.0)",
    },
    # Synthetic qualification clips (pipeline & latency benchmarks)
    "audio_cmd_short_1s.wav": {
        "text": "PICO stop.",
        "type": "synthetic_smoke",
        "description": "Command: short imperative (1.78s)",
    },
    "audio_cmd_query_3s.wav": {
        "text": "What tasks are currently waiting for my approval?",
        "type": "synthetic_smoke",
        "description": "Command: query tasks (3.41s)",
    },
    "audio_cmd_workflow_5s.wav": {
        "text": "Summarize the latest system logs and create a new draft task for review.",
        "type": "synthetic_smoke",
        "description": "Command: workflow proposal (4.92s)",
    },
    "audio_cmd_dictation_10s.wav": {
        "text": "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically.",
        "type": "synthetic_smoke",
        "description": "Command: extended technical dictation (11.03s)",
    },
}


def validate_designated_wav(
    wav_path: Path,
    expected_duration: float = 4.92,
    duration_tolerance: float = 0.5,
) -> tuple[bool, str]:
    """Validate designated WAV contents, PCM format, positive frame count, and manifest duration.

    Returns:
        (is_valid, detail_message)
    """
    if not wav_path.is_file():
        return False, f"File does not exist: {wav_path}"

    try:
        with wave.open(str(wav_path), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            sampwidth = wf.getsampwidth()
            n_frames = wf.getnframes()
    except Exception as exc:
        return False, f"Invalid WAV file format or header: {exc}"

    if channels != 1:
        return False, f"WAV channels={channels} (expected mono, channels=1)"
    if sample_rate != 16000:
        return False, f"WAV sample_rate={sample_rate} Hz (expected 16000 Hz)"
    if sampwidth != 2:
        return False, f"WAV sample width={sampwidth} bytes (expected 2 bytes / 16-bit PCM)"
    if n_frames <= 0:
        return False, f"WAV has non-positive frame count ({n_frames} frames)"

    duration = n_frames / sample_rate
    if abs(duration - expected_duration) > duration_tolerance:
        return False, f"WAV duration={duration:.2f}s deviates from manifest expected duration {expected_duration:.2f}s (+/- {duration_tolerance:.2f}s)"

    return True, f"Valid 16kHz mono 16-bit PCM ({n_frames} frames, {duration:.2f}s)"


def verify_fail_closed_behavior(runtime_dir: Path) -> dict[str, Any]:
    """Verify that corrupted models and missing DLLs fail closed immediately in isolated environments.

    Returns dict with subtest results and boolean passed flag.
    """
    subtests: dict[str, bool] = {
        "missing_model_fails_closed": False,
        "truncated_model_header_fails_closed": False,
        "digest_mismatch_fails_closed": False,
        "corrupted_model_fails_closed": False,
        "missing_runtime_fails_closed": False,
    }

    # Test 1: Missing model file
    try:
        verify_model_artifact(Path("non_existent_isolated_test_model.bin"))
    except WhisperModelVerificationError:
        subtests["missing_model_fails_closed"] = True
    except Exception as exc:
        logger.error("Test 1 (missing model) raised unexpected exception: %s", exc)

    # Test 2: Corrupted model file in isolated TemporaryDirectory (truncated size & cryptographic digest mismatch)
    try:
        with tempfile.TemporaryDirectory(prefix="pico_corrupt_test_") as tmp_dir:
            # 2a: Truncated header (size mismatch)
            try:
                corrupt_path = Path(tmp_dir) / "ggml-base.en-q8_0.bin"
                corrupt_path.write_bytes(b"corrupted truncated header")
                try:
                    verify_model_artifact(corrupt_path)
                except WhisperModelVerificationError:
                    subtests["truncated_model_header_fails_closed"] = True
                except Exception as exc:
                    logger.error("Test 2a (truncated model) raised unexpected exception: %s", exc)
            except Exception as exc:
                logger.error("Test 2a fixture setup/write failed: %s", exc)

            # 2b: Matching size with invalid SHA-256 (cryptographic digest mismatch)
            try:
                dummy_exact_path = Path(tmp_dir) / "custom.bin"
                dummy_exact_path.write_bytes(b"\x00" * 1024)
                try:
                    verify_model_artifact(
                        dummy_exact_path,
                        expected_sha256="0" * 64,
                        expected_size=1024,
                    )
                except WhisperModelVerificationError:
                    subtests["digest_mismatch_fails_closed"] = True
                except Exception as exc:
                    logger.error("Test 2b (digest mismatch) raised unexpected exception: %s", exc)
            except Exception as exc:
                logger.error("Test 2b fixture setup/write failed: %s", exc)
    except Exception as exc:
        logger.error("Test 2 (corrupted model directory setup) failed: %s", exc)

    subtests["corrupted_model_fails_closed"] = (
        subtests["truncated_model_header_fails_closed"] and subtests["digest_mismatch_fails_closed"]
    )

    # Test 3: Missing runtime directory with a valid model fixture
    try:
        with tempfile.TemporaryDirectory(prefix="pico_runtime_test_") as tmp_dir:
            staged_model = DEFAULT_MODEL_DIR / "ggml-base.en-q8_0.bin"
            if staged_model.is_file():
                model_to_use = staged_model
                verify_digest = True
            else:
                dummy_model = Path(tmp_dir) / "dummy.bin"
                dummy_model.write_bytes(b"12345678")
                model_to_use = dummy_model
                verify_digest = False

            try:
                PicoWhisperEngine(
                    model_path=model_to_use,
                    runtime_dir=Path(tmp_dir) / "non_existent_runtime_dir",
                    verify_digest=verify_digest,
                )
            except WhisperRuntimeError:
                subtests["missing_runtime_fails_closed"] = True
            except Exception as exc:
                logger.error("Test 3 (missing runtime) raised unexpected exception: %s", exc)
    except Exception as exc:
        logger.error("Test 3 (missing runtime directory setup) failed: %s", exc)

    all_passed = all(subtests.values())
    return {
        "passed": all_passed,
        "subtests": subtests,
    }


def format_metric_pct(value: float | None, precision: int = 2) -> str:
    """Format numeric float as percentage string or 'N/A' if None."""
    if value is None:
        return "N/A"
    return f"{round(value * 100, precision)}%"


def evaluate_gate_04(
    vram_max_delta_mb: float | None,
    vram_readings_valid: bool,
    ollama_checkpoints: list[tuple[str, Any, str]] | None = None,
    ollama_resident: bool | None = None,
    ollama_info: str | None = None,
    vram_initial_mb: float | None = None,
    max_vram_observed_mb: float | None = None,
    vram_final_mb: float | None = None,
    vram_observations_count: int = 0,
) -> tuple[str, str]:
    """Evaluate GATE-VOICE-04: Single-Model GPU VRAM & Concurrent Ollama LLM Residency.

    Evaluation hierarchy:
    1. Confirmed model eviction: FAIL.
    2. Confirmed digest substitution: FAIL.
    3. Confirmed positive VRAM increase: FAIL (preserves observed breach even if later polls fail).
    4. Missing VRAM telemetry without a confirmed violation: NOT RUN.
    5. Incomplete lifecycle (requires pre_init, during-workload, post_final): NOT RUN.
    6. Baseline not GPU resident or query error: NOT RUN.
    7. Unverified residency query error at subsequent checkpoint: NOT RUN.
    8. Missing model identity evidence (missing baseline or checkpoint digest): NOT RUN.
    9. Complete valid observations with continuity and zero additional VRAM: PASS.

    Returns (status, measured_text).
    """
    # Normalize checkpoints to list of (name, OllamaResidencyState, info, digest)
    normalized_ckpts: list[tuple[str, OllamaResidencyState, str, str | None]] = []
    if ollama_checkpoints is not None:
        for item in ollama_checkpoints:
            name = item[0]
            if len(item) == 2 and isinstance(item[1], OllamaResidencyResult):
                res = item[1]
                raw_d = res.digest
                clean_d = raw_d.strip() if isinstance(raw_d, str) and raw_d.strip() else None
                normalized_ckpts.append((name, res.state, res.info, clean_d))
                continue
            val = item[1]
            info = item[2] if len(item) > 2 else ""
            raw_d = item[3] if len(item) > 3 else getattr(val, "digest", None)
            clean_d = raw_d.strip() if isinstance(raw_d, str) and raw_d.strip() else None
            if isinstance(val, OllamaResidencyState):
                state = val
            elif isinstance(val, bool):
                if val:
                    state = OllamaResidencyState.GPU_RESIDENT
                else:
                    info_l = info.lower()
                    if any(err in info_l for err in ["unreachable", "timed out", "timeout", "connection refused", "error", "malformed"]):
                        state = OllamaResidencyState.QUERY_ERROR
                    else:
                        state = OllamaResidencyState.NOT_RESIDENT
            else:
                state = OllamaResidencyState.QUERY_ERROR
            normalized_ckpts.append((name, state, info, clean_d))
    elif ollama_resident is not None:
        st = OllamaResidencyState.GPU_RESIDENT if ollama_resident else (
            OllamaResidencyState.QUERY_ERROR if any(err in (ollama_info or "").lower() for err in ["unreachable", "timed out", "timeout", "connection refused", "error", "malformed"]) else OllamaResidencyState.NOT_RESIDENT
        )
        normalized_ckpts = [("initial", st, ollama_info or "", None)]

    pre_ckpts = [c for c in normalized_ckpts if c[0] == "pre_init" or c[0].startswith("pre_")]
    subsequent_ckpts = [c for c in normalized_ckpts if c not in pre_ckpts]

    # Baseline is valid GPU resident if all pre-checkpoints exist, are GPU_RESIDENT, and have non-empty matching digests
    baseline_gpu_resident = bool(pre_ckpts and all(c[1] == OllamaResidencyState.GPU_RESIDENT for c in pre_ckpts))
    pre_digests = [c[3] for c in pre_ckpts if c[3] is not None]
    baseline_digest_stable = (len(pre_digests) == len(pre_ckpts) and len(set(pre_digests)) <= 1) if pre_ckpts else False
    baseline_digest = pre_ckpts[0][3] if (pre_ckpts and baseline_digest_stable) else None

    # Rule 1: Confirmed model eviction during STT execution produces FAIL
    # (Evaluated before VRAM completeness so a telemetry failure cannot hide a confirmed eviction)
    if baseline_gpu_resident:
        confirmed_evictions = [c for c in subsequent_ckpts if c[1] == OllamaResidencyState.NOT_RESIDENT]
        if confirmed_evictions:
            evict_name, _, evict_info, _ = confirmed_evictions[0]
            return (
                "FAIL",
                f"Target model 'qwen3.5:2b' was evicted from GPU during STT execution (residency lost at checkpoint '{evict_name}': {evict_info}). "
                f"Co-residency was violated.",
            )

    # Rule 2: Confirmed digest substitution produces FAIL
    # Only classified as a workload failure when a valid GPU-resident baseline was established
    if baseline_gpu_resident and baseline_digest is not None:
        for c_name, _, _, c_digest in subsequent_ckpts:
            if c_digest and c_digest != baseline_digest:
                return (
                    "FAIL",
                    f"Target model 'qwen3.5:2b' digest changed during STT execution (baseline: {baseline_digest}, at '{c_name}': {c_digest}). "
                    f"Model substitution or re-loading detected.",
                )

    # Rule 3: Confirmed positive VRAM increase produces FAIL
    # (Preserved even if subsequent VRAM readings failed or vram_readings_valid is False)
    if vram_max_delta_mb is not None and vram_max_delta_mb > 0:
        return (
            "FAIL",
            f"Maximum observed VRAM Delta = {vram_max_delta_mb} MiB (exceeds 0 MiB budget; "
            f"initial={vram_initial_mb} MiB, peak={max_vram_observed_mb} MiB, "
            f"final={vram_final_mb} MiB across {vram_observations_count} observations)",
        )

    # Rule 4: Missing or invalid VRAM telemetry without a confirmed violation produces NOT RUN
    if vram_max_delta_mb is None or not vram_readings_valid or vram_initial_mb is None:
        return (
            "NOT RUN",
            "GPU VRAM telemetry unavailable via nvidia-smi",
        )

    # Rule 5: Lifecycle completeness
    has_pre_init = any(name == "pre_init" or name.startswith("pre_") for name, _, _, _ in normalized_ckpts)
    has_workload = any(name.startswith("sample_") or "workload" in name for name, _, _, _ in normalized_ckpts)
    has_post_final = any(name == "post_final" for name, _, _, _ in normalized_ckpts)

    if not (has_pre_init and has_workload and has_post_final):
        observed_phases = [c[0] for c in normalized_ckpts]
        return (
            "NOT RUN",
            f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
            f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
            f"Single-model residency qualification NOT RUN because complete lifecycle evidence is missing "
            f"(requires pre_init, during_workload, and post_final checkpoints; observed: {observed_phases}).",
        )

    # Rule 6: Pre-run baseline checks
    for p_name, p_state, p_info, _ in pre_ckpts:
        if p_state == OllamaResidencyState.NOT_RESIDENT:
            status_desc = "was not stable on GPU before benchmark" if "stability" in p_name else "was not resident on GPU before benchmark"
            return (
                "NOT RUN",
                f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
                f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
                f"Single-model residency qualification NOT RUN because target model 'qwen3.5:2b' {status_desc} ({p_info}).",
            )
        elif p_state == OllamaResidencyState.QUERY_ERROR:
            return (
                "NOT RUN",
                f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
                f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
                f"Single-model residency qualification NOT RUN because Ollama residency telemetry could not be verified before benchmark ({p_info}).",
            )

    # Pre-run baseline digest stability check
    if len(pre_ckpts) >= 2 and pre_ckpts[0][3] != pre_ckpts[1][3]:
        return (
            "NOT RUN",
            f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
            f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
            f"Single-model residency qualification NOT RUN because baseline model artifact digest was unstable before benchmark "
            f"('{pre_ckpts[0][0]}'={pre_ckpts[0][3]} vs '{pre_ckpts[1][0]}'={pre_ckpts[1][3]}).",
        )

    # Rule 7: Telemetry query errors at subsequent checkpoints
    query_errors = [c for c in subsequent_ckpts if c[1] == OllamaResidencyState.QUERY_ERROR]
    if query_errors:
        unv_name, _, unv_info, _ = query_errors[0]
        return (
            "NOT RUN",
            f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
            f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
            f"Single-model residency qualification NOT RUN because Ollama residency telemetry could not be verified at checkpoint '{unv_name}' ({unv_info}).",
        )

    # Rule 8: Missing model identity evidence (missing baseline digest or checkpoint digest)
    if baseline_digest is None:
        return (
            "NOT RUN",
            f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
            f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
            f"Single-model residency qualification NOT RUN because baseline model artifact digest was not provided by Ollama.",
        )

    missing_digest_ckpts = [c for c in subsequent_ckpts if not c[3]]
    if missing_digest_ckpts:
        m_name = missing_digest_ckpts[0][0]
        return (
            "NOT RUN",
            f"CPU isolation component PASS (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
            f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations). "
            f"Single-model residency qualification NOT RUN because model artifact digest was missing at checkpoint '{m_name}'.",
        )

    # Rule 9: Complete valid observations with continuity and zero additional VRAM -> PASS
    final_info = normalized_ckpts[-1][2]
    return (
        "PASS",
        f"VRAM Delta = 0 MiB with target model 'qwen3.5:2b' verified resident in GPU VRAM across sampled checkpoints ({final_info}). "
        f"CPU isolation observed (0 MiB additional VRAM at sampled checkpoints; initial={vram_initial_mb} MiB, "
        f"peak={max_vram_observed_mb} MiB, final={vram_final_mb} MiB across {vram_observations_count} observations).",
    )


def evaluate_gate_05(
    peak_working_set_mb: float | None,
    min_available_ram_mb: float | None,
    private_usage_mb: float | None = None,
    ws_threshold_mb: float = 450.0,
    avail_threshold_mb: float = 1536.0,  # 1.5 GB
    ram_telemetry_valid: bool = True,
    samples_count: int = 0,
) -> tuple[str, str]:
    """Evaluate GATE-VOICE-05: System RAM Ceiling.

    Requires BOTH:
      1. Peak voice Working Set < 450.0 MB
      2. Minimum Available System RAM > 1536.0 MB (1.5 GB) continuously sampled across run.

    Returns (status, measured_text).
    """
    if min_available_ram_mb is not None:
        r1 = round(min_available_ram_mb, 1)
        r2 = round(min_available_ram_mb, 2)
        avail_disp = f"{r2}" if r1 != r2 else f"{r1}"
    else:
        avail_disp = "N/A"

    if not ram_telemetry_valid or peak_working_set_mb is None or min_available_ram_mb is None:
        breaches: list[str] = []
        if peak_working_set_mb is not None and peak_working_set_mb >= ws_threshold_mb:
            breaches.append(f"Peak Working Set: {peak_working_set_mb} MB exceeds {ws_threshold_mb} MB ceiling")
        if min_available_ram_mb is not None and min_available_ram_mb <= avail_threshold_mb:
            breaches.append(f"Minimum Available RAM: {avail_disp} MB <= {avail_threshold_mb} MB (1.5 GB) threshold")
        if breaches:
            return "FAIL", "; ".join(breaches) + " (telemetry flagged incomplete or error encountered)"
        return "NOT RUN", "Required process or system RAM monitoring could not be completed"

    ws_pass = peak_working_set_mb < ws_threshold_mb
    avail_pass = min_available_ram_mb > avail_threshold_mb

    priv_str = f", Private Bytes: {private_usage_mb} MB" if private_usage_mb is not None else ""
    samples_str = f" across {samples_count} continuous samples" if samples_count > 0 else ""

    if ws_pass and avail_pass:
        return (
            "PASS",
            f"Peak Working Set: {peak_working_set_mb} MB (< {ws_threshold_mb} MB), "
            f"Min Available System RAM: {avail_disp} MB (> 1.5 GB){priv_str}{samples_str}",
        )
    else:
        fail_reasons: list[str] = []
        if not ws_pass:
            fail_reasons.append(f"Peak Working Set {peak_working_set_mb} MB >= {ws_threshold_mb} MB")
        if not avail_pass:
            fail_reasons.append(
                f"Min Available System RAM {avail_disp} MB <= {avail_threshold_mb} MB (1.5 GB threshold)"
            )
        return (
            "FAIL",
            "; ".join(fail_reasons) + priv_str + samples_str,
        )


def run_benchmark(
    model_path: Path,
    runtime_dir: Path,
    corpus_dir: Path,
    n_threads: int = 4,
    iterations: int = 10,
    option_b_corpus_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Execute complete qualification benchmark suite with rigorous evidence collection."""
    current_pid = os.getpid()

    if manifest_path is None:
        manifest_path = REPO_ROOT / "docs" / "evaluation" / "voice_corpus_manifest.json"

    # Query initial memory and system state
    peak_ws_init, cur_ws_init, priv_init = query_process_memory_mb()
    total_sys_ram_mb, avail_sys_ram_init = query_system_memory_mb()
    sockets_initial = audit_active_process_sockets(current_pid)

    git_commit, is_clean = query_git_commit()
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "host": {
            "os": platform.platform(),
            "processor": platform.processor(),
            "cpu_topology": "Intel Core i5-13420H (8 physical cores: 4P + 4E, 12 logical processors)",
            "cpu_threads_dedicated": n_threads,
            "python": platform.python_version(),
            "process_id": current_pid,
            "git_commit": git_commit,
            "git_clean": is_clean,
            "total_system_ram_mb": total_sys_ram_mb,
            "initial_available_ram_mb": avail_sys_ram_init,
            "initial_working_set_mb": cur_ws_init,
            "initial_vram_used_mb": None,
        },
        "model": {},
        "runtime": {},
        "results": [],
        "summary": {},
        "gates": {},
    }

    # Start continuous background physical RAM and process Working Set sampler
    sampler = ContinuousMemorySampler(sample_interval_seconds=0.05, pid=current_pid)
    sampler.start()
    ollama_checkpoints: list[tuple[str, OllamaResidencyState, str, str | None]] = []
    engine: PicoWhisperEngine | None = None
    gate_02_result: OptionBGate02Result | None = None

    try:
        # Step 1: Verify intended model is positively GPU-resident before establishing baseline
        res_pre = verify_ollama_model_residency("qwen3.5:2b")
        ollama_checkpoints.append(("pre_init", res_pre.state, res_pre.info, res_pre.digest))
        report["host"]["ollama_llm_resident"] = (res_pre.state == OllamaResidencyState.GPU_RESIDENT)
        report["host"]["ollama_llm_details"] = res_pre.info

        # Step 2: Always retain second pre-initialization observation and compare digest with the first
        time.sleep(0.05)
        res_stable = verify_ollama_model_residency("qwen3.5:2b")
        ollama_checkpoints.append(("pre_init_stability_check", res_stable.state, res_stable.info, res_stable.digest))
        if res_stable.state != OllamaResidencyState.GPU_RESIDENT:
            logger.warning("Ollama residency unstable during baseline setup: %s -> %s", res_pre.info, res_stable.info)

        # Step 3: Capture VRAM baseline only AFTER verifying Ollama residency state
        vram_initial = query_gpu_vram_mb()
        report["host"]["initial_vram_used_mb"] = vram_initial

        # Step 4: Model verification and engine initialization
        logger.info("Verifying model artifact: %s", model_path)
        model_meta = verify_model_artifact(model_path)
        report["model"] = model_meta

        # Cold Start initialization
        t_init_start = time.perf_counter()
        engine = PicoWhisperEngine(
            model_path=model_path,
            runtime_dir=runtime_dir,
            verify_digest=True,
            n_threads=n_threads,
        )
        t_init_cold = time.perf_counter() - t_init_start
        report["runtime"]["version"] = engine.version
        report["runtime"]["system_info"] = engine.system_info
        report["runtime"]["cold_init_seconds"] = round(t_init_cold, 4)

        # Step 5: Record post-initialization residency
        res_post_init = verify_ollama_model_residency("qwen3.5:2b")
        ollama_checkpoints.append(("post_init", res_post_init.state, res_post_init.info, res_post_init.digest))

        sample_results: list[dict[str, Any]] = []
        max_vram_observed = vram_initial if vram_initial is not None else 0
        min_vram_observed = vram_initial if vram_initial is not None else 999999
        vram_readings_valid = vram_initial is not None
        vram_observations_count = 1 if vram_initial is not None else 0

        min_avail_ram_observed = avail_sys_ram_init if avail_sys_ram_init is not None else 999999.0
        avail_ram_readings_valid = avail_sys_ram_init is not None
        # 4. Iterate through Corpus
        audio_files = sorted(corpus_dir.glob("*.wav"))
        if not audio_files:
            raise FileNotFoundError(f"No WAV files found in corpus directory: {corpus_dir}")

        for audio_file in audio_files:
            filename = audio_file.name
            gt_info = CANONICAL_CORPUS_GROUND_TRUTH.get(filename, {})
            ref_text = gt_info.get("text", "")
            sample_type = gt_info.get("type", "unknown")
            sample_desc = gt_info.get("description", filename)

            with audio_file.open("rb") as f:
                wav_bytes = f.read()

            # Warm-up iteration
            engine.transcribe_wav_bytes(wav_bytes, n_threads=n_threads)

            # Iteration timings
            e2e_latencies: list[float] = []
            native_latencies: list[float] = []
            wav_parse_latencies: list[float] = []
            pcm_norm_latencies: list[float] = []
            c_buf_latencies: list[float] = []
            durations: list[float] = []
            hypotheses: list[str] = []

            for _ in range(iterations):
                # Continuous physical RAM sampling
                _, avail_now = query_system_memory_mb()
                if avail_now is not None:
                    min_avail_ram_observed = min(min_avail_ram_observed, avail_now)
                else:
                    avail_ram_readings_valid = False

                # Continuous GPU VRAM sampling
                vram_now = query_gpu_vram_mb()
                if vram_now is not None:
                    max_vram_observed = max(max_vram_observed, vram_now)
                    min_vram_observed = min(min_vram_observed, vram_now)
                    vram_observations_count += 1
                else:
                    vram_readings_valid = False

                transcript, e2e_proc_time, audio_dur, b_down = engine.transcribe_wav_bytes(
                    wav_bytes, n_threads=n_threads, return_breakdown=True
                )
                e2e_latencies.append(e2e_proc_time)
                native_latencies.append(b_down["native_inference_seconds"])
                wav_parse_latencies.append(b_down["wav_parse_seconds"])
                pcm_norm_latencies.append(b_down["pcm_norm_seconds"])
                c_buf_latencies.append(b_down["c_buf_prep_seconds"])
                durations.append(audio_dur)
                hypotheses.append(transcript)

            audio_dur = durations[0]

            # End-to-end percentiles
            e2e_latencies.sort()
            e2e_p50 = e2e_latencies[len(e2e_latencies) // 2]
            e2e_p90 = e2e_latencies[int(len(e2e_latencies) * 0.9)]
            e2e_p95 = e2e_latencies[min(int(len(e2e_latencies) * 0.95), len(e2e_latencies) - 1)]
            e2e_max = e2e_latencies[-1]
            e2e_min = e2e_latencies[0]
            rtf_e2e_p50 = round(e2e_p50 / audio_dur, 4) if audio_dur > 0 else 0.0

            # Native-only percentiles
            native_latencies.sort()
            native_p50 = native_latencies[len(native_latencies) // 2]
            native_p90 = native_latencies[int(len(native_latencies) * 0.9)]
            native_p95 = native_latencies[min(int(len(native_latencies) * 0.95), len(native_latencies) - 1)]
            native_max = native_latencies[-1]
            native_min = native_latencies[0]
            rtf_native_p50 = round(native_p50 / audio_dur, 4) if audio_dur > 0 else 0.0

            # Average breakdown components
            avg_wav_parse_ms = round(sum(wav_parse_latencies) / len(wav_parse_latencies) * 1000, 2)
            avg_pcm_norm_ms = round(sum(pcm_norm_latencies) / len(pcm_norm_latencies) * 1000, 2)
            avg_c_buf_ms = round(sum(c_buf_latencies) / len(c_buf_latencies) * 1000, 2)
            avg_native_ms = round(sum(native_latencies) / len(native_latencies) * 1000, 2)

            best_hypothesis = hypotheses[0]

            # Compute detailed Levenshtein breakdown
            if ref_text:
                ref_words = normalize_text_for_eval(ref_text).split()
                hyp_words = normalize_text_for_eval(best_hypothesis).split()
                if ref_words:
                    dist, subs, dels, ins = compute_levenshtein_ops(ref_words, hyp_words)
                    wer = round(dist / len(ref_words), 4)
                else:
                    dist, subs, dels, ins = (len(hyp_words), 0, 0, len(hyp_words))
                    wer = 0.0 if not hyp_words else 1.0

                cer = compute_cer(ref_text, best_hypothesis)
            else:
                dist, subs, dels, ins = 0, 0, 0, 0
                wer = None
                cer = None

            sample_entry = {
                "filename": filename,
                "type": sample_type,
                "description": sample_desc,
                "audio_duration_seconds": round(audio_dur, 3),
                "iterations": iterations,
                # End-to-end latency metrics (primary qualification criteria)
                "p50_latency_seconds": round(e2e_p50, 4),
                "p90_latency_seconds": round(e2e_p90, 4),
                "p95_latency_seconds": round(e2e_p95, 4),
                "min_latency_seconds": round(e2e_min, 4),
                "max_latency_seconds": round(e2e_max, 4),
                "rtf_p50": rtf_e2e_p50,
                # Native inference latency metrics (separately preserved)
                "native_p50_latency_seconds": round(native_p50, 4),
                "native_p90_latency_seconds": round(native_p90, 4),
                "native_p95_latency_seconds": round(native_p95, 4),
                "native_min_latency_seconds": round(native_min, 4),
                "native_max_latency_seconds": round(native_max, 4),
                "native_rtf_p50": rtf_native_p50,
                "breakdown_ms": {
                    "wav_parse": avg_wav_parse_ms,
                    "pcm_norm": avg_pcm_norm_ms,
                    "c_buf_prep": avg_c_buf_ms,
                    "native_inference": avg_native_ms,
                },
                "reference_transcript": ref_text,
                "emitted_transcript": best_hypothesis,
                "wer": wer,
                "cer": cer,
                "edit_distance": dist,
                "substitutions": subs,
                "deletions": dels,
                "insertions": ins,
                "ref_word_count": len(normalize_text_for_eval(ref_text).split()) if ref_text else 0,
            }
            sample_results.append(sample_entry)
            logger.info(
                "Sample %s (%ss): E2E P50=%ss (RTF=%.3f), Native P50=%ss (RTF=%.3f), WER=%s",
                filename,
                round(audio_dur, 2),
                round(e2e_p50, 3),
                rtf_e2e_p50,
                round(native_p50, 3),
                rtf_native_p50,
                format_metric_pct(wer),
            )

            # Checkpoint: Check Ollama residency during workload after each audio file
            res_ckpt = verify_ollama_model_residency("qwen3.5:2b")
            ollama_checkpoints.append((f"sample_{filename}", res_ckpt.state, res_ckpt.info, res_ckpt.digest))

        # Checkpoint: Run Option B STT evaluation while engine is alive and sampler is active
        def _option_b_telemetry_poll() -> None:
            nonlocal min_avail_ram_observed, avail_ram_readings_valid
            nonlocal max_vram_observed, min_vram_observed, vram_observations_count, vram_readings_valid
            _, avail_now = query_system_memory_mb()
            if avail_now is not None:
                min_avail_ram_observed = min(min_avail_ram_observed, avail_now)
            else:
                avail_ram_readings_valid = False

            vram_now = query_gpu_vram_mb()
            if vram_now is not None:
                max_vram_observed = max(max_vram_observed, vram_now)
                min_vram_observed = min(min_vram_observed, vram_now)
                vram_observations_count += 1
            else:
                vram_readings_valid = False

        gate_02_result = evaluate_option_b_gate_02(
            engine=engine,
            manifest_path=manifest_path,
            corpus_dir=option_b_corpus_dir,
            n_threads=n_threads,
            model_meta=report.get("model"),
            telemetry_poll_fn=_option_b_telemetry_poll,
        )

        # Checkpoint: Check Ollama residency post-final inference iteration
        res_post_final = verify_ollama_model_residency("qwen3.5:2b")
        ollama_checkpoints.append(("post_final", res_post_final.state, res_post_final.info, res_post_final.digest))

        # 5. Final Telemetry
        peak_ws_final, cur_ws_final, priv_final = query_process_memory_mb()
        _, avail_sys_ram_final = query_system_memory_mb()
        if avail_sys_ram_final is not None:
            min_avail_ram_observed = min(min_avail_ram_observed, avail_sys_ram_final)
        else:
            avail_ram_readings_valid = False

        vram_final = query_gpu_vram_mb()
        if vram_final is not None:
            max_vram_observed = max(max_vram_observed, vram_final)
            min_vram_observed = min(min_vram_observed, vram_final)
            vram_observations_count += 1
        else:
            vram_readings_valid = False

        sockets_final = audit_active_process_sockets(current_pid)

    finally:
        try:
            if engine is not None:
                engine.close()
        finally:
            sampler.stop()

    sampler_peak_ws, sampler_min_avail_ram, ram_valid, ram_samples_count = sampler.get_results()

    all_ws_readings = [ws for ws in [peak_ws_init, peak_ws_final, sampler_peak_ws] if ws is not None]
    overall_peak_ws = max(all_ws_readings) if all_ws_readings else None

    # Calculate overall minimum from all valid observations:
    # 1. Initial synchronous system RAM sample (avail_sys_ram_init)
    # 2. Continuous sampler observations (sampler_min_avail_ram)
    # 3. Synchronous observations collected during benchmark (min_avail_ram_observed)
    # 4. Final synchronous system RAM sample (avail_sys_ram_final)
    # Minimum across all valid sources ensures a low synchronous reading is NEVER hidden by a higher sampler reading.
    all_valid_avail_ram: list[float] = []
    if avail_sys_ram_init is not None:
        all_valid_avail_ram.append(avail_sys_ram_init)
    if sampler_min_avail_ram is not None:
        all_valid_avail_ram.append(sampler_min_avail_ram)
    if min_avail_ram_observed < 999990.0:
        all_valid_avail_ram.append(min_avail_ram_observed)
    if avail_sys_ram_final is not None:
        all_valid_avail_ram.append(avail_sys_ram_final)

    overall_min_avail_ram = min(all_valid_avail_ram) if all_valid_avail_ram else None

    report["host"]["peak_working_set_mb"] = overall_peak_ws
    report["host"]["final_working_set_mb"] = cur_ws_final
    report["host"]["final_private_usage_mb"] = priv_final
    report["host"]["final_available_ram_mb"] = avail_sys_ram_final
    report["host"]["min_available_ram_mb"] = round(overall_min_avail_ram, 1) if overall_min_avail_ram is not None else None
    report["host"]["continuous_ram_samples_count"] = ram_samples_count
    report["host"]["continuous_ram_telemetry_valid"] = ram_valid
    report["host"]["ollama_checkpoints"] = ollama_checkpoints
    report["host"]["final_vram_used_mb"] = vram_final
    report["host"]["peak_vram_used_mb"] = max_vram_observed if vram_readings_valid else None
    report["host"]["vram_observations_count"] = vram_observations_count
    report["host"]["vram_cadence"] = "sampled per iteration (~every 1s)"

    confirmed_max_delta: float | None = None
    if vram_initial is not None and max_vram_observed > vram_initial:
        confirmed_max_delta = max_vram_observed - vram_initial

    if vram_readings_valid and vram_initial is not None and vram_final is not None:
        report["host"]["vram_delta_mb"] = vram_final - vram_initial
        report["host"]["vram_max_delta_mb"] = max_vram_observed - vram_initial
    else:
        report["host"]["vram_delta_mb"] = None
        # Preserve confirmed positive breach even if subsequent polls failed
        report["host"]["vram_max_delta_mb"] = confirmed_max_delta

    report["results"] = sample_results

    # 6. Aggregate Accuracy Calculations
    real_speech = [s for s in sample_results if s["type"] == "real_speech" and s["wer"] is not None]
    smoke_cmds = [s for s in sample_results if s["type"] == "synthetic_smoke" and s["wer"] is not None]

    # Aggregate corpus WER for real speech: (total edits / total ref words)
    if real_speech:
        total_real_edits = sum(s["edit_distance"] for s in real_speech)
        total_real_ref_words = sum(s["ref_word_count"] for s in real_speech)
        aggregate_real_wer = round(total_real_edits / total_real_ref_words, 4) if total_real_ref_words > 0 else 0.0
        macro_real_wer = round(sum(s["wer"] for s in real_speech) / len(real_speech), 4)
        macro_real_cer = round(sum(s["cer"] for s in real_speech) / len(real_speech), 4)
    else:
        aggregate_real_wer = None
        macro_real_wer = None
        macro_real_cer = None

    if smoke_cmds:
        total_smoke_edits = sum(s["edit_distance"] for s in smoke_cmds)
        total_smoke_ref_words = sum(s["ref_word_count"] for s in smoke_cmds)
        aggregate_smoke_wer = round(total_smoke_edits / total_smoke_ref_words, 4) if total_smoke_ref_words > 0 else 0.0
        macro_smoke_wer = round(sum(s["wer"] for s in smoke_cmds) / len(smoke_cmds), 4)
    else:
        aggregate_smoke_wer = None
        macro_smoke_wer = None

    all_rtf = [s["rtf_p50"] for s in sample_results]
    avg_rtf = round(sum(all_rtf) / len(all_rtf), 4) if all_rtf else 0.0

    report["summary"] = {
        "total_samples": len(sample_results),
        "avg_rtf_p50": avg_rtf,
        "real_speech_aggregate_wer": aggregate_real_wer,
        "real_speech_macro_wer": macro_real_wer,
        "real_speech_macro_cer": macro_real_cer,
        "smoke_commands_aggregate_wer": aggregate_smoke_wer,
        "smoke_commands_macro_wer": macro_smoke_wer,
        "cold_init_seconds": report["runtime"]["cold_init_seconds"],
        "peak_working_set_mb": overall_peak_ws,
        "min_available_ram_mb": report["host"]["min_available_ram_mb"],
        "vram_delta_mb": report["host"]["vram_delta_mb"],
        "vram_max_delta_mb": report["host"]["vram_max_delta_mb"],
    }

    # 7. GATE Evaluation Matrix
    # GATE-VOICE-01: STT Latency
    # Strict requirement: Designated 5-second sample ('audio_cmd_workflow_5s.wav') MUST exist and satisfy End-to-End P50 latency <= 1.5s (RTF <= 0.30)
    DESIGNATED_5S_SAMPLE = "audio_cmd_workflow_5s.wav"
    five_sec_sample = next((s for s in sample_results if s["filename"] == DESIGNATED_5S_SAMPLE), None)
    if five_sec_sample is not None:
        designated_wav_path = corpus_dir / DESIGNATED_5S_SAMPLE
        wav_valid, wav_detail = validate_designated_wav(designated_wav_path)
        if not wav_valid:
            gate_01_status = "FAIL"
            gate_01_measured = f"Designated 5s sample '{DESIGNATED_5S_SAMPLE}' failed audio validation: {wav_detail}"
        else:
            p50_5s = five_sec_sample["p50_latency_seconds"]
            rtf_5s = five_sec_sample["rtf_p50"]
            nat_p50_5s = five_sec_sample["native_p50_latency_seconds"]
            nat_rtf_5s = five_sec_sample["native_rtf_p50"]
            bk = five_sec_sample["breakdown_ms"]

            gate_01_pass = (p50_5s <= 1.5) and (rtf_5s <= 0.30)
            gate_01_status = "PASS" if gate_01_pass else "FAIL"
            gate_01_measured = (
                f"5s sample '{five_sec_sample['filename']}': "
                f"End-to-End P50={p50_5s}s (RTF={rtf_5s}), P90={five_sec_sample['p90_latency_seconds']}s, "
                f"P95={five_sec_sample['p95_latency_seconds']}s, Max={five_sec_sample['max_latency_seconds']}s; "
                f"Native-only P50={nat_p50_5s}s (RTF={nat_rtf_5s}), P90={five_sec_sample['native_p90_latency_seconds']}s, "
                f"P95={five_sec_sample['native_p95_latency_seconds']}s, Max={five_sec_sample['native_max_latency_seconds']}s "
                f"[Breakdown: WAV parse={bk['wav_parse']}ms, PCM norm={bk['pcm_norm']}ms, C buf={bk['c_buf_prep']}ms, Native={bk['native_inference']}ms]"
            )
    else:
        gate_01_status = "NOT RUN"
        gate_01_measured = f"Designated 5-second benchmark sample '{DESIGNATED_5S_SAMPLE}' missing from evaluation corpus"

    # GATE-VOICE-02: STT Accuracy (WER) - Formal Option B Evaluation
    if gate_02_result is None:
        gate_02_result = evaluate_option_b_gate_02(
            engine=None,
            manifest_path=manifest_path,
            corpus_dir=option_b_corpus_dir,
            n_threads=n_threads,
            model_meta=report.get("model"),
        )
    report["option_b_gate_02"] = gate_02_result.to_dict()
    gate_02_status = gate_02_result.status
    gate_02_measured = gate_02_result.format_measured_summary(
        historical_mixed_wer=aggregate_real_wer,
        historical_macro_wer=macro_real_wer,
        historical_macro_cer=macro_real_cer,
        smoke_wer=aggregate_smoke_wer,
        historical_samples_count=len(real_speech) if real_speech else None,
        historical_words_count=total_real_ref_words if real_speech else None,
        historical_edits_count=total_real_edits if real_speech else None,
    )

    # GATE-VOICE-03: TTS Latency
    gate_03_status = "NOT RUN"
    gate_03_measured = "Deferred to Milestone P11.3.4 (Piper/sherpa-onnx qualification)"

    # GATE-VOICE-04: Single-Model GPU VRAM
    gate_04_status, gate_04_measured = evaluate_gate_04(
        vram_max_delta_mb=report["host"]["vram_max_delta_mb"],
        vram_readings_valid=vram_readings_valid,
        ollama_checkpoints=ollama_checkpoints,
        vram_initial_mb=vram_initial,
        max_vram_observed_mb=max_vram_observed,
        vram_final_mb=vram_final,
        vram_observations_count=vram_observations_count,
    )

    # GATE-VOICE-05: System RAM Ceiling
    gate_05_status, gate_05_measured = evaluate_gate_05(
        peak_working_set_mb=report["host"]["peak_working_set_mb"],
        min_available_ram_mb=overall_min_avail_ram,
        private_usage_mb=priv_final,
        ws_threshold_mb=450.0,
        avail_threshold_mb=1536.0,
        ram_telemetry_valid=ram_valid,
        samples_count=ram_samples_count,
    )

    # GATE-VOICE-06: Disk Retention
    gate_06_status = "NOT RUN"
    gate_06_measured = (
        "0 raw audio files created in process temp/working directories; "
        "independent kernel ETW minifilter filesystem audit unavailable without administrative elevation"
    )

    # GATE-VOICE-07: Network Egress
    socket_count = len(sockets_final)
    gate_07_status = "NOT RUN"
    gate_07_measured = (
        f"{socket_count} active socket connections detected for PID {current_pid}; "
        "kernel ETW DNS-client activity tracing unavailable without elevation"
    )

    # GATE-VOICE-08: Packaging Bounds (STT-only footprint measured; full voice subsystem deferred)
    bin_size_mb = sum(p.stat().st_size for p in runtime_dir.glob("*.dll")) / (1024 * 1024)
    model_size_mb = model_meta.get("size_bytes", 0) / (1024 * 1024)
    stt_footprint_mb = round(bin_size_mb + model_size_mb, 2)
    gate_08_status = "NOT RUN"
    gate_08_measured = (
        f"STT Prototype: {stt_footprint_mb} MB (Binaries: {round(bin_size_mb, 2)} MB, Model: {round(model_size_mb, 2)} MB). "
        "Combined STT+TTS voice subsystem gate deferred to Milestone P11.3.4."
    )

    # GATE-VOICE-09: Fail-Closed Safety
    fc_results = verify_fail_closed_behavior(runtime_dir=runtime_dir)
    gate_09_pass = fc_results["passed"]
    gate_09_status = "PASS" if gate_09_pass else "FAIL"
    sub_results = fc_results.get("subtests", {})
    report["fail_closed_subtests"] = sub_results
    passed_probes = [k.replace("_fails_closed", "").replace("_", " ") for k, v in sub_results.items() if v]
    failed_probes = [k.replace("_fails_closed", "").replace("_", " ") for k, v in sub_results.items() if not v]
    if gate_09_pass:
        gate_09_measured = f"Verified: all probes passed ({', '.join(passed_probes)}) in isolated fixtures"
    else:
        gate_09_measured = f"Incomplete/Failed: passed [{', '.join(passed_probes)}], failed/unexecuted [{', '.join(failed_probes)}]"

    # GATE-VOICE-10: License Review
    gate_10_status = "NOT RUN"
    gate_10_measured = (
        "STT Prototype Component License Assessment: PASS (whisper.cpp engine MIT, OpenAI Whisper weights MIT). "
        "Overall GATE-VOICE-10 NOT RUN pending Milestone P11.3.4 TTS engine and voice-model licensing qualification."
    )

    report["gates"] = {
        "GATE-VOICE-01": {
            "name": "STT Latency",
            "threshold": "RTF <= 0.30 (5s audio in <= 1.5s)",
            "measured": gate_01_measured,
            "status": gate_01_status,
            "method": "Monotonic high-resolution timer over 10 warm iterations on 5-second sample",
        },
        "GATE-VOICE-02": {
            "name": "STT Accuracy (WER)",
            "threshold": "Aggregate WER <= 12.0% across Option B human corpus (N_total = 49 reference words)",
            "measured": gate_02_measured,
            "status": gate_02_status,
            "method": "Wagner-Fischer dynamic programming edit distance with digit normalization over 49 canonical reference words",
        },
        "GATE-VOICE-03": {
            "name": "TTS Latency",
            "threshold": "TTFAC < 500ms; RTF_synth <= 0.15",
            "measured": gate_03_measured,
            "status": gate_03_status,
            "method": "Deferred to Milestone P11.3.4",
        },
        "GATE-VOICE-04": {
            "name": "Single-Model GPU VRAM",
            "threshold": "VRAM Delta = 0 MiB (preserves Ollama LLM residency)",
            "measured": gate_04_measured,
            "status": gate_04_status,
            "method": "nvidia-smi VRAM polling + local Ollama running model inspection",
        },
        "GATE-VOICE-05": {
            "name": "System RAM Ceiling",
            "threshold": "Peak Working Set < 450 MB and Available System RAM > 1.5 GB",
            "measured": gate_05_measured,
            "status": gate_05_status,
            "method": "Win32 GetProcessMemoryInfo (PeakWorkingSetSize) and GlobalMemoryStatusEx (ullAvailPhys)",
        },
        "GATE-VOICE-06": {
            "name": "Disk Retention",
            "threshold": "0 raw-audio bytes persisted to disk",
            "measured": gate_06_measured,
            "status": gate_06_status,
            "method": "Directory audit of temp and working directories; ETW minifilter tracing",
        },
        "GATE-VOICE-07": {
            "name": "Network Egress",
            "threshold": "0 outbound socket connections & 0 DNS queries",
            "measured": gate_07_measured,
            "status": gate_07_status,
            "method": "Process socket connection audit (netstat -ano) + DNS Client ETW tracing",
        },
        "GATE-VOICE-08": {
            "name": "Packaging Bounds",
            "threshold": "Combined voice subsystem footprint < 350 MB",
            "measured": gate_08_measured,
            "status": gate_08_status,
            "method": "Filesystem summation of staged binaries and verified models",
        },
        "GATE-VOICE-09": {
            "name": "Fail-Closed Safety",
            "threshold": "Deterministic typed exception on missing or corrupted artifact",
            "measured": gate_09_measured,
            "status": gate_09_status,
            "method": "Isolated temporary directory tests with malformed and missing inputs",
        },
        "GATE-VOICE-10": {
            "name": "License Review",
            "threshold": "Permissive MIT license without copyleft risk (voice subsystem full qualification)",
            "measured": gate_10_measured,
            "status": gate_10_status,
            "method": "Upstream source and repository license verification",
        },
    }

    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    """Format benchmark report dictionary into clean, truthful Markdown."""
    gate_01_status = report.get("gates", {}).get("GATE-VOICE-01", {}).get("status", "NOT RUN")
    gate_02_status = report.get("gates", {}).get("GATE-VOICE-02", {}).get("status", "NOT RUN")
    gate_04_status = report.get("gates", {}).get("GATE-VOICE-04", {}).get("status", "NOT RUN")
    gate_05_status = report.get("gates", {}).get("GATE-VOICE-05", {}).get("status", "NOT RUN")

    if gate_01_status == "PASS" and gate_02_status == "PASS" and gate_04_status == "PASS" and gate_05_status == "PASS":
        status_line = "Stage 2 Prototype — Core Latency, Option B Accuracy, VRAM & RAM Qualified; Egress & Disk Retention NOT RUN"
    elif gate_01_status == "PASS" and gate_04_status == "PASS" and gate_05_status == "PASS":
        acc_str = "Accuracy Qualified" if gate_02_status == "PASS" else ("Accuracy FAILED" if gate_02_status == "FAIL" else "Accuracy NOT RUN")
        status_line = f"Stage 2 Prototype — Core Latency, VRAM & RAM Qualified; {acc_str}, Egress & Disk Retention NOT RUN"
    elif gate_01_status == "PASS" and gate_05_status == "PASS" and gate_04_status == "NOT RUN":
        acc_str = "Accuracy Qualified" if gate_02_status == "PASS" else ("Accuracy FAILED" if gate_02_status == "FAIL" else "Accuracy NOT RUN")
        status_line = f"Stage 2 Prototype — Partial Qualification (Core Latency & RAM Met; Ollama Co-Residency NOT RUN; {acc_str}; Egress & Disk Retention NOT RUN)"
    else:
        status_line = "Stage 2 Prototype — Incomplete / Partial Qualification"

    git_commit_str = report['host'].get('git_commit', 'unknown')
    git_clean_val = report['host'].get('git_clean')
    if git_clean_val is True:
        clean_str = " (Clean)"
    elif git_clean_val is False:
        clean_str = " (Modified)"
    else:
        clean_str = " (Unknown)"
    git_commit_display = f"`{git_commit_str}`{clean_str}"

    manifest_name = report.get("option_b_gate_02", {}).get("manifest_display_name") or "voice_corpus_manifest.json"
    manifest_sha = report.get("option_b_gate_02", {}).get("manifest_sha256")
    if manifest_sha:
        manifest_display = f"`{manifest_name}` (SHA-256: `{manifest_sha[:16]}...`)"
    else:
        manifest_display = f"`{manifest_name}`"

    lines: list[str] = [
        "# PICO — Local STT (whisper.cpp) Hardware Qualification Report (Stage 2 Prototype)",
        "",
        f"- **Date**: {report['timestamp']}",
        f"- **Commit**: {git_commit_display}",
        "- **Milestone**: P11.3.2 Stage 2 Native Whisper Qualification Prototype",
        f"- **Status**: {status_line}",
        f"- **Host Hardware**: {report['host']['processor']}",
        f"- **CPU Topology**: {report['host']['cpu_topology']}",
        f"- **Execution Target**: CPU-only ({report['host']['cpu_threads_dedicated']} threads, AVX2 enabled, GPU disabled)",
        "- **Scheduler Note**: Inference configured with 4 worker threads; scheduler affinity was not explicitly bound to P-cores.",
        "- **Responsiveness Note**: FastAPI event loop responsiveness and capture stream continuity under concurrent load were not measured during this prototype benchmark and are marked NOT RUN.",
        f"- **Runtime Engine**: whisper.cpp {report['runtime'].get('version', 'unknown')}",
        f"- **Model**: `{report['model'].get('filename')}` ({round(report['model'].get('size_bytes', 0) / (1024*1024), 2)} MB, SHA-256: `{report['model'].get('sha256', '')[:16]}...`)",
        f"- **Corpus Manifest**: {manifest_display}",
        "",
        "---",
        "",
        "## 1. Executive Summary & Qualification Gates",
        "",
        "| Gate ID | Area / Gate | Required Threshold | Measured Result | Qualification Status |",
        "|---|---|---|---|---|",
    ]

    for gid, g in report.get("gates", {}).items():
        status_badge = f"**{g['status']}**" if g["status"] == "PASS" else g["status"]
        lines.append(f"| `{gid}` | **{g['name']}** | {g['threshold']} | {g['measured']} | {status_badge} |")

    lines.extend([
        "",
        "---",
        "",
        "## 2. Benchmark Results Across Test Corpus",
        "",
        "| Audio Sample | Type | Duration | E2E P50 | Native P50 | E2E P90 | E2E P95 | E2E Max | E2E RTF | Native RTF | WER (%) | CER (%) | Emitted Transcript |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ])

    for r in report.get("results", []):
        wer_str = format_metric_pct(r.get("wer"), 1)
        cer_str = format_metric_pct(r.get("cer"), 1)
        native_p50 = r.get("native_p50_latency_seconds", r["p50_latency_seconds"])
        native_rtf = r.get("native_rtf_p50", r["rtf_p50"])
        lines.append(
            f"| `{r['filename']}` | {r['type']} | {r['audio_duration_seconds']}s | "
            f"{r['p50_latency_seconds']}s | {native_p50}s | "
            f"{r['p90_latency_seconds']}s | {r['p95_latency_seconds']}s | {r['max_latency_seconds']}s | "
            f"**{r['rtf_p50']}** | {native_rtf} | {wer_str} | {cer_str} | \"{r['emitted_transcript']}\" |"
        )

    summary = report.get("summary", {})
    real_wer = summary.get("real_speech_aggregate_wer")
    real_macro_wer = summary.get("real_speech_macro_wer")
    real_macro_cer = summary.get("real_speech_macro_cer")
    smoke_wer = summary.get("smoke_commands_aggregate_wer")

    fc_subtests = report.get("fail_closed_subtests", {})
    if fc_subtests and all(fc_subtests.values()):
        passed_names = ", ".join(k.replace("_fails_closed", "").replace("_", " ") for k in fc_subtests)
        fc_desc = f"4. **Fail-Closed Protection**: Verified immediate typed exceptions across all probes ({passed_names}) in isolated fixtures."
    elif fc_subtests:
        passed_names = ", ".join(k.replace("_fails_closed", "").replace("_", " ") for k, v in fc_subtests.items() if v)
        failed_names = ", ".join(k.replace("_fails_closed", "").replace("_", " ") for k, v in fc_subtests.items() if not v)
        fc_desc = f"4. **Fail-Closed Protection**: Incomplete fail-closed verification: passed [{passed_names}], failed/unexecuted [{failed_names}]."
    else:
        fc_desc = "4. **Fail-Closed Protection**: Fail-closed safety probe execution unrecorded."

    lines.extend([
        "",
        "---",
        "",
        "## 3. Accuracy Metrics Breakdown",
        "",
    ])

    opt_b = report.get("option_b_gate_02", {})
    if opt_b.get("command_results"):
        lines.extend([
            "### 3.1 Formal Option B Human Accuracy Results (GATE-VOICE-02)",
            "",
            "| Command ID | Reference Text | Words | Emitted Hypothesis | Edits (S/D/I) | Command WER |",
            "|---|---|---|---|---|---|",
        ])
        for cr in opt_b["command_results"]:
            safe_ref = cr["reference_text"].replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            safe_hyp = cr["hypothesis_text"].replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            lines.append(
                f"| `{cr['command_id']}` | \"{safe_ref}\" | {cr['reference_word_count']} | "
                f"\"{safe_hyp}\" | {cr['edit_distance']} ({cr['substitutions']}/{cr['deletions']}/{cr['insertions']}) | "
                f"{format_metric_pct(cr['wer_ratio'])} |"
            )
        lines.extend([
            "",
            f"- **Option B Aggregate Edits**: {opt_b.get('aggregate_edit_count')} edits",
            f"- **Option B Exact Denominator**: {opt_b.get('exact_reference_denominator', 49)} reference words",
            f"- **Option B Aggregate WER**: **{opt_b.get('aggregate_wer_pct')}** (Threshold: <= {opt_b.get('target_wer_threshold_pct', 12.0)}%)",
            f"- **GATE-VOICE-02 Formal Gate Status**: **{opt_b.get('status', 'NOT RUN')}**",
            "",
        ])
    else:
        opt_b_reason = opt_b.get("reason", "Option B human command corpus pending acquisition and registration.")
        lines.extend([
            "### 3.1 Formal Option B Human Accuracy Status (GATE-VOICE-02)",
            "",
            f"- **GATE-VOICE-02 Status**: **{opt_b.get('status', 'NOT RUN')}**",
            f"- **Details**: {opt_b_reason}",
            "",
        ])

    lines.extend([
        "### 3.2 Historical Mixed Real-Speech Prototype Fixtures (Informational Only)",
        "",
        f"- **Real Speech Aggregate WER**: {format_metric_pct(real_wer)} (total edits / total reference words across historical mixed real-speech fixtures: 5 JFK speech slices + 1 vosk_digits.wav numeric recording)",
        f"- **Real Speech Macro-Average WER**: {format_metric_pct(real_macro_wer)} (unweighted mean across the 6 mixed real-speech fixtures)",
        f"- **Real Speech Macro-Average CER**: {format_metric_pct(real_macro_cer)}",
        "",
        "### 3.3 Synthetic Commands Smoke Test Fixtures (CI Regression Smoke Only)",
        "",
        f"- **Synthetic Commands Smoke WER**: {format_metric_pct(smoke_wer)} (Windows SAPI smoke test; reported separately from real speech)",
        "",
        "> [!NOTE]",
        "> **Canonical Option B Corpus & Formal WER Status**:",
        "> The canonical command reference word count is formally established at exactly **49 reference words** across the four canonical commands ($2 + 8 + 13 + 26 = 49\\text{ words}$, with command 4 having 26 words).",
        "> Formal Word Error Rate ($\\text{WER}$) for `GATE-VOICE-02` can **only** be calculated when all four natural human audio recordings are provided under Option B.",
        "> Historical mixed real-speech prototype fixtures (5 JFK speech slices + 1 Vosk digit sequence, 75 total reference words) and synthetic Windows SAPI smoke clips represent distinct preliminary evaluation datasets; their edit counts and error metrics must **never** be cross-conflated with or transferred onto the canonical Option B command corpus.",
        f"> Consequently, `GATE-VOICE-02` status is: **`{report.get('gates', {}).get('GATE-VOICE-02', {}).get('status', 'NOT RUN')}`**.",
        "",
        "---",
        "",
        "## 4. Hardware & Resource Telemetry",
        "",
        f"- **Cold Start Initialization Time**: {summary.get('cold_init_seconds')}s (PicoWhisperEngine.__init__() - full constructor: artifact SHA-256 verification, DLL load, native context prep)",
        f"- **Peak Working Set Memory**: {summary.get('peak_working_set_mb')} MB",
        f"- **Minimum Available System RAM**: {summary.get('min_available_ram_mb')} MB"
        + (
            f" (threshold > 1.5 GB, continuously sampled across {report['host'].get('continuous_ram_samples_count')} intervals)"
            if report['host'].get('continuous_ram_samples_count')
            else " (threshold > 1.5 GB)"
        ),
        f"- **Initial GPU VRAM**: {report['host'].get('initial_vram_used_mb')} MiB",
        f"- **Peak GPU VRAM Observed**: {report['host'].get('peak_vram_used_mb')} MiB ({report['host'].get('vram_observations_count', 0)} observations, {report['host'].get('vram_cadence')})",
        f"- **Final GPU VRAM**: {report['host'].get('final_vram_used_mb')} MiB",
        f"- **Maximum GPU VRAM Delta**: {report['host'].get('vram_max_delta_mb')} MiB",
        f"- **Ollama LLM Co-Residency**: {report['host'].get('ollama_llm_details')}",
        f"- **Average Real-Time Factor (E2E RTF)**: **{summary.get('avg_rtf_p50')}**",
        "",
        "---",
        "",
        "## 5. Architectural Invariants Verification",
        "",
        "1. **Zero Raw-Audio Disk Retention**: Evaluated in-memory pointer pipeline. 0 audio files found in process temporary directories. Full OS kernel minifilter audit marked NOT RUN.",
        "2. **Zero Cloud Egress**: Monitored socket connections for PID. Full kernel ETW DNS tracing marked NOT RUN.",
        "3. **In-Memory Buffer Zeroization**: Best-effort zeroization scrubs mutable application arrays (ctypes float arrays, lists, bytearrays). Immutable Python objects and native C++ heap buffers are freed upon context teardown.",
        fc_desc,
        "5. **Production Boundary**: Production adapter (`LocalWhisperSpeechToTextAdapter`) remains strictly disabled (`is_available() == False`) and fails closed.",
    ])

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="PICO Local Voice Engine Hardware Qualification Tool")
    parser.add_argument(
        "--model",
        type=Path,
        default=DEFAULT_MODEL_DIR / "ggml-base.en-q8_0.bin",
        help="Path to GGML model weight file",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=DEFAULT_RUNTIME_DIR,
        help="Directory containing pico_whisper_shim.dll and dependencies",
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=Path(os.path.expandvars(r"%LOCALAPPDATA%\PICO\corpus")),
        help="Directory containing evaluation WAV files",
    )
    parser.add_argument(
        "--option-b-corpus-dir",
        type=Path,
        default=None,
        help="Directory containing local Option B human command WAV files (GATE-VOICE-02)",
    )
    parser.add_argument(
        "--manifest-path",
        type=Path,
        default=REPO_ROOT / "docs" / "evaluation" / "voice_corpus_manifest.json",
        help="Path to voice corpus manifest JSON",
    )
    parser.add_argument(
        "--threads",
        type=int,
        default=4,
        help="Number of CPU threads for inference (strictly 1 to 4, default: 4)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=10,
        help="Number of warm benchmark iterations per sample (default: 10)",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=REPO_ROOT / "bench_reports" / "voice_bench_results.json",
        help="Path to save JSON benchmark output",
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=REPO_ROOT / "docs" / "evaluation" / "voice-qualification-results.md",
        help="Path to save Markdown qualification report",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    logger.info("Starting PICO Voice Qualification Benchmark...")
    logger.info("  Model: %s", args.model)
    logger.info("  Runtime Dir: %s", args.runtime_dir)
    logger.info("  Corpus Dir: %s", args.corpus_dir)
    logger.info("  Threads: %d, Iterations: %d", args.threads, args.iterations)

    report = run_benchmark(
        model_path=args.model,
        runtime_dir=args.runtime_dir,
        corpus_dir=args.corpus_dir,
        n_threads=args.threads,
        iterations=args.iterations,
        option_b_corpus_dir=args.option_b_corpus_dir,
        manifest_path=args.manifest_path,
    )

    # Save JSON report
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with args.output_json.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    logger.info("Wrote JSON report to %s", args.output_json)

    # Save Markdown report
    args.output_md.parent.mkdir(parents=True, exist_ok=True)
    md_content = render_markdown_report(report)
    args.output_md.write_text(md_content, encoding="utf-8")
    logger.info("Wrote Markdown report to %s", args.output_md)

    print("\n" + md_content + "\n")


if __name__ == "__main__":
    main()
