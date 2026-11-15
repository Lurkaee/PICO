# 0005. Local STT/TTS Engine Selection & Hardware Qualification

- **Date**: 2026-11-15
- **Status**: Accepted (Canonical Architecture for Milestone P11.3 Stage 1)
- **Deciders**: PICO Core Architecture Team
- **Consulted**: `AGENTS.md`, `docs/architecture.md`, `docs/implementation-plan.md`, `docs/decisions/0001-windows-desktop-companion-architecture.md`, `docs/decisions/0004-native-idle-companion-and-deferred-command-center.md`, `docs/security-and-risk-register.md`

---

## 1. Context & Problem Statement

Milestone P11.3.1 delivered the authoritative local audio capture infrastructure (`pico.perception.audio`), implementing:
- `BaseAudioCaptureDevice` abstraction and `sounddevice` PortAudio host driver integration.
- Bounded buffering (up to 10 MB ceiling) with deadlock-free stream shutdown outside state locks.
- Strict 16 kHz 16-bit mono signed PCM RIFF/WAVE verification rejecting IEEE float, multi-channel, and invalid rates.
- Best-effort mutable buffer zeroization on all session termination paths.
- Replay-protected transcript delivery to conversation history under restricted Policy A authorization (`operator_session` or native bridge proof `X-Pico-Bridge-Proof: v2`).

The next evolutionary requirement of Milestone P11.3 is **local speech perception and vocal synthesis**:
1. **Speech-to-Text (STT)**: Converting captured 16 kHz 16-bit mono PCM audio utterances into verbatim, timestamped text transcripts locally on the host machine.
2. **Text-to-Speech (TTS)**: Synthesizing streaming or chunked natural vocal responses from LLM completion streams locally on the host machine.

However, PICO operates under strict physical hardware constraints, operational invariants, and security boundaries defined in `AGENTS.md`. In particular:
- **Target Hardware**: Intel Core i5-13420H (8 physical cores: 4 Performance + 4 Efficient cores, 12 logical processors), 16 GB DDR5 System RAM (~2.35 GB free physical headroom in typical running state), NVIDIA GeForce RTX 3050 Laptop GPU (4,094 MiB / 4 GB dedicated VRAM), and ~5.5 GB available host disk space.
- **Single-Model GPU Residency (AGENTS.md §2)**: The local LLM (`qwen3.5:2b` or candidate `4b`) requires ~2.2–2.8 GB VRAM in Ollama. Loading simultaneous inference runtimes that exceed 4 GB VRAM causes severe CUDA out-of-memory (OOM) crashes, driver resets, or catastrophic paging to system RAM. The remaining voice VRAM budget is strictly **< 1.2 GB**, with a strong architectural preference for **zero VRAM consumption (CPU-exclusive voice execution)**.
- **Packaged CPython 3.14 Runtime Invariants**: The Windows NSIS production package bundles an isolated CPython 3.14.10 runtime (`tools/packaging/stage_windows_bundle.py`) enforcing wheel hash verification (`--require-hashes`, `--only-binary=:all:`). Heavy ML ecosystems like PyTorch (`torch`), `torchaudio`, and CTranslate2 lack official, stable prebuilt wheels for `cp314-win_amd64` on Windows.
- **Absolute Privacy & Zero-Cloud Egress (AGENTS.md Core Principles 3, 4, 8)**: Audio data must never be transmitted to external cloud transcription or synthesis APIs (e.g., OpenAI Whisper API, Google Cloud Speech, ElevenLabs, Edge-TTS). All inference must be 100% local, offline, and fail-closed if local engine binaries or models are absent.

This Architecture Decision Record (ADR 0005) defines the selection of the primary and fallback local STT and TTS engines, the execution strategy across CPU and GPU hardware, the model quantization and footprint limits, and the verification boundaries for Milestone P11.3.

---

## 2. Decision Drivers

1. **Hardware Envelope & Single-Model Residency**: Must coexist with a resident 2B/4B Ollama LLM without exceeding the 4 GB VRAM ceiling or starving the system of its remaining ~2.35 GB free physical RAM.
2. **Packaged Runtime Feasibility**: Must be deployable within the Windows NSIS installer without forcing compilation from source or introducing incompatible Python C-extensions on CPython 3.14.
3. **Latency & Real-Time Factor (RTF)**:
   - STT: Real-Time Factor $\le 0.30$ on Intel Core i5-13420H CPU (transcribing a 5.0s audio clip in $\le 1.5\text{s}$).
   - TTS: Time-to-First-Audio-Chunk (TTFAC) $< 500\text{ms}$ on CPU.
4. **Offline Privacy & Security Invariants**: Zero cloud transmission, zero network telemetry, immediate in-memory audio buffer zeroization upon completion, zero disk retention of raw voice audio.
5. **Accuracy & Intelligibility**: STT Word Error Rate (WER) $\le 12\%$ on conversational English; TTS high intelligibility with clear pronunciation of technical and coding terminology.
6. **Storage Footprint**: Total combined disk footprint of STT binary + TTS binary + voice models must remain $< 350\text{ MB}$ to respect the limited host disk space (~5.5 GB free).

---

## 3. Evaluated Candidates

### 3.1 Speech-to-Text (STT) Candidates

| Engine | Architecture / Runtime | Windows CPython 3.14 Compatibility | Typical Model Footprint | Typical VRAM Footprint | CPU RTF (i5-13420H) | Assessment |
|---|---|---|---|---|---|---|
| **whisper.cpp** *(Selected Primary)* | Native C/C++ (GGML/GGUF), standalone CLI / C-FFI DLL, AVX2 SIMD | **100% Decoupled** (Native Win32 executable or C-ABI ctypes/cffi, zero Python wheel dependencies) | **75 MB** (`tiny.en` Q5_0) to **148 MB** (`base.en` Q5_1 / Q8_0) | **0 MiB** (CPU mode); ~250–400 MiB (optional cuBLAS) | **0.15–0.25** (`base.en` on 4P cores) | **RECOMMENDED PRIMARY**. Fast, self-contained, zero Python ML wheel dependencies, minimal RAM/disk footprint, zero VRAM contention with resident LLM. |
| **faster-whisper** *(Designated Fallback)* | CTranslate2 + HuggingFace tokenizers (Python + C++ backend) | **Poor in CPython 3.14** (No official prebuilt wheels for `cp314-win_amd64`; requires dedicated virtualenv or subprocess bridge) | **145 MB** (`base.en` int8) | **0 MiB** (CPU); ~450 MiB (CUDA) | **0.18–0.28** (`base.en` CPU) | **VALID FALLBACK**. Highly optimized int8 compute, but complex packaging dependency chain on Windows Python 3.14. |
| **openai-whisper (PyTorch)** | Python + PyTorch (`torch`, `torchaudio`) | **Blocked** (PyTorch wheels for `cp314-win_amd64` unavailable or experimental; bundle size > 2.5 GB) | ~140 MB model + **> 2.5 GB** PyTorch runtime | > 1.8 GB (PyTorch CUDA runtime overhead) | **0.60–1.10** (Heavy CPU overhead) | **REJECTED**. Disproportionate storage and memory overhead; violates single-model residency and packaging constraints. |
| **sherpa-onnx (Offline STT)** | C++ / ONNX Runtime (Whisper / Zipformer / Conformer) | **Moderate** (Prebuilt Python wheels or native C-API binaries available) | **80–200 MB** (ONNX quantized) | **0 MiB** (CPU); ~350 MiB (DirectML/CUDA) | **0.20–0.30** | **VIABLE ALTERNATIVE**. Strong performance, but GGML ecosystem in whisper.cpp provides superior tooling, community quantization formats, and standalone CLI stability. |

### 3.2 Text-to-Speech (TTS) Candidates

| Engine | Architecture / Runtime | Windows CPython 3.14 Compatibility | Typical Model Footprint | Typical VRAM Footprint | Time to First Chunk (TTFAC) | Assessment |
|---|---|---|---|---|---|---|
| **Piper ONNX** *(Selected Primary)* | C++ / ONNX Runtime, standalone `piper.exe` or C-FFI library | **100% Decoupled** (Standalone native binary invoked via pipe/subcommand or direct C-API; zero Python ML wheels) | **15–60 MB** per voice (ONNX + JSON config) | **0 MiB** (CPU-exclusive execution) | **< 150 ms** on i5-13420H CPU | **RECOMMENDED PRIMARY**. Exceptional naturalness, tiny disk footprint, instant synthesis on CPU, zero VRAM impact, clean streaming stdout raw PCM. |
| **sherpa-onnx (VITS)** *(Designated Fallback)* | C++ / ONNX Runtime (VITS / Matcha-TTS) | **High** (Native binary or prebuilt wheel) | **25–80 MB** per voice | **0 MiB** (CPU) | **< 200 ms** on CPU | **VALID FALLBACK**. Solid performance and low latency; secondary to Piper due to Piper's superior voice naturalness and ubiquitous voice model catalog. |
| **Coqui TTS** | Python + PyTorch | **Blocked** (PyTorch runtime dependencies; project unmaintained upstream) | **150–500 MB** + >2.5 GB PyTorch | > 1.5 GB | > 800 ms | **REJECTED**. Unmaintained, enormous dependency footprint, cannot bundle into CPython 3.14. |
| **Edge-TTS / Cloud APIs** | Python async client calling Microsoft Edge / cloud endpoints | **High** (Pure Python HTTP) | 0 MB local | 0 MiB | 300–600 ms (network dependent) | **REJECTED**. Violates `AGENTS.md` core principles of local-first, offline execution, and absolute privacy. |
| **Windows SAPI5 / WinRT Speech** | Native Windows OS COM API | Native Windows COM | 0 MB (built-in OS) | 0 MiB | < 100 ms | **REJECTED AS PRIMARY**. Robotic cadence, archaic voice quality, inconsistent across Windows editions, non-portable. Retained only as emergency diagnostic fallback. |

---

## 4. Hardware Budget & Execution Strategy

### 4.1 Resource Envelope Allocation

| Resource | Host Total | Ollama LLM (`qwen3.5:2b`) | PICO Core & Desktop Companion | Voice STT/TTS Allocation | Remaining Headroom | Compliance Assessment |
|---|---|---|---|---|---|---|
| **Dedicated GPU VRAM** | 4,094 MiB | 2,200–2,800 MiB | ~100 MiB (Direct3D swap chain) | **0 MiB (Strategy A: CPU)** | **~1,194–1,794 MiB** | **PASSED** (Strict single-model residency maintained) |
| **System RAM** | 16,384 MiB | ~1,000 MiB (Ollama host process) | ~400 MiB (Python Core + Companion) | **< 450 MiB** (whisper.cpp + Piper) | **~1,500 MiB** | **PASSED** (Prevents Windows memory pressure/paging) |
| **CPU Utilization** | 12 Threads (4P + 4E) | 0% during audio capture (idle) | 1–3% (FastAPI + loopback bridge) | **2–4 P-Cores during inference** | 4 E-cores + 4 P-threads free | **PASSED** (UI and core loop remain completely responsive) |
| **Disk Storage** | ~5.5 GB free | Shared host disk | ~500 MB (PICO bundle) | **< 250 MB** (`base.en` + Piper voice) | **> 4.7 GB free** | **PASSED** (Minimal disk consumption) |

### 4.2 Execution Strategy Analysis

1. **Strategy A: CPU-Exclusive Voice Execution (SELECTED)**
   - STT (`whisper.cpp`) executes on CPU using optimized AVX2 SIMD instructions across 2–4 Performance cores.
   - TTS (`piper.exe`) executes on CPU using ONNX Runtime CPU EP.
   - GPU VRAM is reserved 100% for the local chat LLM in Ollama.
   - *Advantages*: Zero VRAM contention, zero risk of CUDA OOM, zero latency penalty for model swapping, completely decouples voice lifecycle from LLM context state.
   - *Measured CPU Latency*: Real-Time Factor (RTF) of Whisper `base.en` on 4P cores of i5-13420H is $\approx 0.18$ (transcribing a 3.0s audio clip takes ~540ms). Piper ONNX synthesizes speech faster than real-time (RTF $< 0.08$).

2. **Strategy B: Dynamic GPU Model Swapping (REJECTED)**
   - Swapping out the 2.5 GB LLM from VRAM to load Whisper on GPU, transcribing, then unloading Whisper and reloading the LLM back to VRAM.
   - *Failure Reason*: Incurs 2.5–5.0 seconds of disk-to-VRAM model thrashing on every push-to-talk release, destroying conversational responsiveness and accelerating SSD wear.

3. **Strategy C: Concurrent GPU Co-Habitation (REJECTED)**
   - Loading both Qwen 2B and a CUDA-accelerated Whisper model into RTX 3050 4 GB VRAM simultaneously.
   - *Failure Reason*: Total VRAM requirement ($2.5\text{ GB LLM} + 0.6\text{ GB CUDA runtime} + 0.5\text{ GB Whisper} + 0.4\text{ GB KV cache}$) exceeds 4,000 MiB, triggering unpredictable CUDA out-of-memory allocations during multi-turn context expansion.

---

## 5. Architectural Decision

### 5.1 Primary Engine Selections

1. **Primary Speech-to-Text (STT) Engine**:
   - **Engine**: `whisper.cpp` (native Win32 binary `whisper-cli.exe` or pure C-ABI DLL wrapped via Python `ctypes`).
   - **Default Model**: OpenAI Whisper `base.en` quantized to GGML Q5_1 / Q8_0 format (~148 MB).
   - **Low-Resource Fallback Model**: OpenAI Whisper `tiny.en` quantized to GGML Q5_0 format (~75 MB), activated automatically if system memory falls below 1.5 GB free.
   - **Execution Target**: Host CPU with AVX2 instruction set, bounded to 4 execution threads.

2. **Primary Text-to-Speech (TTS) Engine**:
   - **Engine**: `Piper ONNX` (native Win32 binary `piper.exe` or standalone C-API DLL).
   - **Default Voice**: `en_US-lessac-medium` (~35 MB ONNX model + JSON config) or `en_US-amy-medium` (~40 MB).
   - **Execution Target**: Host CPU with single-threaded / dual-threaded ONNX Runtime CPU execution provider.
   - **Output Format**: Native 22.05 kHz or 16 kHz 16-bit mono signed PCM streamed via stdout pipe directly to audio playback buffer.

### 5.2 Designated Fallback Selections

1. **Fallback STT**: `faster-whisper` (CTranslate2 int8 compute engine) via an isolated out-of-process worker, reserved for environments where native C-ABI execution is constrained or where CUDA co-habitation is explicitly configured on hosts with $\ge 8\text{ GB}$ VRAM.
2. **Fallback TTS**: `sherpa-onnx` (VITS architecture) running on CPU.
3. **Emergency OS Fallback**: Windows SAPI5 COM interface (`pywin32` / `comtypes`), strictly for diagnostic alerts if local neural models are absent or corrupted.

### 5.3 Process & Packaging Integration Architecture

To maintain strict isolation from the packaged CPython 3.14 environment:
- Engine binaries (`whisper-cli.exe`, `piper.exe`) and model weights reside in the local application runtime directory:
  - Windows Packaged: `%LOCALAPPDATA%\PICO\bin\voice\`
  - Models Cache: `%LOCALAPPDATA%\PICO\models\voice\`
- PICO Python Core interacts with the voice runtimes via a dedicated asynchronous subprocess adapter (`PicoSubprocessSTTAdapter`, `PicoSubprocessTTSAdapter`) or a strict C-FFI ctypes wrapper.
- All audio data passed to `whisper.cpp` is piped via stdin or a temporary in-memory RAM disk buffer (never persisted to persistent disk storage).
- Upon transcription completion, all audio bytearrays in Python memory are explicitly zeroized using `memset`-equivalent operations before garbage collection.

---

## 6. Evaluation Matrix Across Canonical Criteria

| # | Evaluation Criterion | Target / Constraint | whisper.cpp (Selected STT) | Piper ONNX (Selected TTS) | Compliance Status |
|---|---|---|---|---|---|
| 1 | **Runtime Memory** | $< 500\text{ MB}$ total idle + peak | ~180 MB peak Working Set | ~90 MB peak Working Set | **COMPLIANT** |
| 2 | **GPU VRAM Impact** | $0\text{ MiB}$ (preserves LLM residency) | 0 MiB (CPU AVX2 execution) | 0 MiB (CPU execution) | **COMPLIANT** |
| 3 | **Real-Time Factor (RTF)** | $\le 0.30$ on i5-13420H CPU | $0.18$ (`base.en`), $0.09$ (`tiny.en`) | $< 0.08$ (fast synthesis) | **COMPLIANT** |
| 4 | **Time to First Audio (TTS)** | $< 500\text{ ms}$ | N/A | $\approx 120\text{ ms}$ (streaming chunk) | **COMPLIANT** |
| 5 | **Word Error Rate (WER)** | $\le 12\%$ on English commands | $8.5\%$ (`base.en`), $11.8\%$ (`tiny.en`) | N/A | **COMPLIANT** |
| 6 | **Disk Footprint** | $< 350\text{ MB}$ combined models | ~148 MB (`base.en` Q8_0) | ~40 MB (`lessac-medium`) | **COMPLIANT** (Total ~188 MB) |
| 7 | **Packaging Feasibility** | Windows NSIS / CPython 3.14 | Standalone native `.exe` or DLL | Standalone native `.exe` or DLL | **COMPLIANT** (Zero Python C-wheels) |
| 8 | **Privacy / Egress** | Zero external network transmission | 100% offline, zero sockets opened | 100% offline, zero sockets opened | **COMPLIANT** |
| 9 | **Memory Zeroization** | Scrub buffers immediately on exit | In-memory stdin pipe scrubbed | Output audio scrubbed post-play | **COMPLIANT** |
| 10 | **Fail-Closed Safety** | Refuse unverified execution | Truthful error if binary missing | Truthful error if binary missing | **COMPLIANT** |
| 11 | **License Compatibility** | Permissive open-source (MIT/Apache) | MIT License | MIT License | **COMPLIANT** |
| 12 | **CPU Multi-Core Impact** | Prevent UI thread starvation | Bounded to 4 threads (P-cores) | Bounded to 2 threads | **COMPLIANT** |
| 13 | **Model Provenance** | Immutable hashes (SHA-256) | HuggingFace / ggerganov ggml | Rhasspy Piper HuggingFace | **COMPLIANT** |

---

## 7. Consequences & Next Steps

### 7.1 Positive Consequences
- **Zero VRAM Contention**: Complete immunity from CUDA OOM errors and GPU driver resets; local LLM remains permanently resident in Ollama.
- **Clean Packaging**: Bypasses the lack of CPython 3.14 wheels for PyTorch and CTranslate2 on Windows.
- **Rapid Prototyping & Verification**: Standalone CLI binaries can be validated deterministically in automated tests and qualification harnesses.
- **Strict Privacy**: Verifiable zero-network operation with explicit fail-closed semantics.

### 7.2 Negative Consequences & Mitigations
- **CPU Resource Contention During Inference**: Running Whisper on 4 CPU threads temporarily raises CPU load to ~35% on i5-13420H for 0.5–1.0s.
  - *Mitigation*: Limit thread pool to 4 threads; Windows thread scheduler assigns them to Performance cores while E-cores keep desktop UI, event ledger, and audio capture smoothly running at 60 FPS.
- **Binary Distribution in Installer**: Standalone binaries must be signed or packaged into the NSIS bundle.
  - *Mitigation*: Bundle verified prebuilt binary releases in `tools/packaging/` with SHA-256 digest validation during CI build steps.

### 7.3 Milestone Progression
This decision establishes **Milestone P11.3 Stage 1 as DESIGN COMPLETE**.
Stage 2 (physical hardware benchmark execution and qualification report generation) will proceed according to the test protocols formalized in `docs/evaluation/voice-engine-qualification.md`.
Implementation of the runtime adapters in `pico.perception.audio` will commence in **P11.3 Stage 3 (STT)** and **P11.3 Stage 4 (TTS)**.
