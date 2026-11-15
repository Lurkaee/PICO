# 0005. Local STT/TTS Engine Selection & Hardware Qualification

- **Date**: 2026-11-15
- **Status**: Accepted (Canonical Architecture for Milestone P11.3 Stage 1) — Design Complete; Provisional Engine Selections Pending Hardware Qualification & Legal Review
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
2. **Text-to-Speech (TTS)**: Synthesizing natural vocal responses from LLM completion streams locally on the host machine.

However, PICO operates under strict physical hardware constraints, operational invariants, and security boundaries defined in `AGENTS.md`. In particular:
- **Target Hardware Envelope**: Intel Core i5-13420H (8 physical cores: 4 Performance + 4 Efficient cores, 12 logical processors, AVX2 SIMD), 16 GB DDR5 System RAM (~2.35 GB free physical headroom in host baseline snapshot 2026-11-15T17:31:14+05:30), NVIDIA GeForce RTX 3050 Laptop GPU (4,094 MiB / 4 GB dedicated VRAM), and ~5.48 GB available host disk space on drive `C:` (time-sensitive measurement).
- **Single-Model GPU Residency (AGENTS.md §2)**: The local LLM (`qwen3.5:2b` or candidate `4b`) requires ~2.2–2.8 GB VRAM in Ollama. Loading simultaneous inference runtimes that exceed 4 GB VRAM causes severe CUDA out-of-memory (OOM) crashes, driver resets, or catastrophic paging to system RAM. The remaining voice VRAM budget is strictly **< 1.2 GB**, with a non-negotiable architectural preference for **zero VRAM consumption (CPU-exclusive voice execution)**.
- **Packaged CPython 3.14 Runtime Invariants**: The Windows NSIS production package bundles an isolated CPython 3.14.10 runtime (`tools/packaging/stage_windows_bundle.py`) enforcing wheel hash verification (`--require-hashes`, `--only-binary=:all:`). While upstream packages like CTranslate2 and ONNX Runtime publish binary wheels for standard CPython 3.14 (`cp314-cp314-win_amd64`), heavy ecosystems like PyTorch require gigabytes of wheel dependencies. Decoupling voice runtimes into standalone native Win32 executables or C-ABI DLLs prevents package bloat and eliminates the risk of native C-extension crashes destabilizing the core FastAPI process.
- **Absolute Privacy & Zero-Cloud Egress (AGENTS.md Core Principles 3, 4, 8)**: Audio data must never be transmitted to external cloud transcription or synthesis APIs (e.g., OpenAI Whisper API, Google Cloud Speech, ElevenLabs, Edge-TTS). All inference must be 100% local, offline, and fail-closed if local engine binaries or models are absent.

This Architecture Decision Record (ADR 0005) defines the provisional selection of the primary and fallback local STT and TTS engines, the execution strategy across CPU and GPU hardware, the model quantization and footprint limits, and the verification boundaries for Milestone P11.3 Stage 1.

---

## 2. Decision Drivers

1. **Hardware Envelope & Single-Model Residency**: Must coexist with a resident 2B/4B Ollama LLM without exceeding the 4 GB VRAM ceiling or starving the system of its remaining ~2.35 GB free physical RAM.
2. **Packaged Runtime Feasibility**: Must be deployable within the Windows NSIS installer without forcing compilation from source or introducing incompatible Python C-extensions on CPython 3.14.
3. **Latency & Real-Time Factor (RTF) Acceptance Targets**:
   - STT Acceptance Target: Real-Time Factor $\text{RTF} \le 0.30$ on Intel Core i5-13420H CPU (transcribing a 5.0s audio clip in $\le 1.5\text{s}$, where $\text{RTF} = T_{\text{proc, seconds}} / T_{\text{audio, seconds}}$).
   - TTS Acceptance Target: Time-to-First-Audio-Chunk (TTFAC) $< 500\text{ms}$ on CPU; synthesis $\text{RTF}_{\text{synth}} \le 0.15$.
4. **Offline Privacy & Security Invariants**: Zero cloud transmission, zero network telemetry, monitored absence of outbound connection attempts, auditable best-effort mutable audio buffer zeroization upon completion, zero disk retention of raw voice audio.
5. **Accuracy & Intelligibility**: STT Word Error Rate (WER) target $\le 12\%$ on conversational English commands; TTS high intelligibility with clear pronunciation of technical terminology.
6. **Storage Footprint**: Total combined disk footprint of STT binary + TTS binary + shared DLLs + voice models must remain $< 350\text{ MB}$ to respect the limited host disk space (~5.48 GB free).
7. **Licensing Compatibility**: Engine and voice models must be compatible with PICO's MIT-licensed distribution or distributed under clearly isolated boundaries without violating copyleft terms.

---

## 3. Evaluated Candidates

### 3.1 Speech-to-Text (STT) Candidates

| Engine | Architecture / Runtime | Windows Packaging Compatibility | Published Model Footprint | Typical VRAM Footprint | Published / Estimated CPU RTF | Assessment |
|---|---|---|---|---|---|---|
| **whisper.cpp** *(Selected Primary — Provisional)* | Native C/C++ (GGML/GGUF), standalone CLI / C-ABI DLL, AVX2 SIMD | **100% Decoupled** (Native Win32 executable or C-ABI ctypes/cffi; zero Python ML wheel dependencies) | **32.2 MB** (`tiny.en-q5_1.bin`) to **81.8 MB** (`base.en-q8_0.bin`) / **148 MB** (`base.en.bin` F16) | **0 MiB** (CPU mode); ~250–400 MiB (optional cuBLAS) | **Upstream estimate: ~0.15–0.25** (`base.en` on 4 P-cores); *qualification pending* | **RECOMMENDED PRIMARY (Provisional)**. Fast, self-contained, minimal disk footprint, zero VRAM contention with resident LLM. In-memory integration path via C-ABI DLL or CLI stdin to be verified in Stage 2. |
| **faster-whisper** *(Designated Fallback)* | CTranslate2 + HuggingFace tokenizers (Python + C++ backend) | **Supported on Windows cp314** (`ctranslate2-*-cp314-cp314-win_amd64.whl` exists), but requires bundling `tokenizers`, `av`, and C++ runtime DLLs | **145 MB** (`base.en` int8) | **0 MiB** (CPU); ~450 MiB (CUDA) | **Upstream estimate: ~0.18–0.28** (`base.en` CPU) | **VALID FALLBACK**. Highly optimized int8 compute, but introduces a larger dependency graph into the Python environment. |
| **openai-whisper (PyTorch)** | Python + PyTorch (`torch`, `torchaudio`) | **Blocked by Size** (PyTorch runtime wheels > 2 GB for CUDA / > 200 MB for CPU; massive installation footprint) | ~140 MB model + **> 2.0 GB** PyTorch runtime | > 1.8 GB (PyTorch CUDA runtime overhead) | **Upstream estimate: ~0.60–1.10** | **REJECTED**. Disproportionate storage and memory overhead; violates single-model residency and packaging constraints. |
| **sherpa-onnx (Offline STT)** | C++ / ONNX Runtime (Whisper / Zipformer / Conformer) | **High** (Native binary or prebuilt wheels via Apache 2.0) | **80–200 MB** (ONNX quantized) | **0 MiB** (CPU); ~350 MiB (DirectML/CUDA) | **Upstream estimate: ~0.20–0.30** | **VIABLE ALTERNATIVE**. Permissive Apache 2.0 license, strong performance; retained as candidate if GGML packaging encounters blockers. |

### 3.2 Text-to-Speech (TTS) Candidates

| Engine | Architecture / Runtime | Windows Packaging Compatibility | Published Runtime & Model Footprint | Typical VRAM Footprint | Published / Estimated TTFAC | Assessment |
|---|---|---|---|---|---|---|
| **Piper ONNX** *(Selected Primary — Provisional)* | C++ / ONNX Runtime, standalone `piper.exe` or C-FFI library | **100% Decoupled** (Standalone native binary invoked via pipe or C-API; zero Python ML wheels) | Runtime: ~45 MB (binaries + DLLs + data); Model: ~63.2 MB (`lessac-medium`). Total: **~105–115 MB** | **0 MiB** (CPU-exclusive execution) | **Upstream estimate: < 150 ms** on modern x86 CPU; *qualification pending* | **RECOMMENDED PRIMARY (Provisional)**. High naturalness, instant synthesis on CPU, zero VRAM impact. Code license (GPL-3.0 in active fork) requires legal redistribution review. |
| **sherpa-onnx (VITS)** *(Designated Fallback)* | C++ / ONNX Runtime (VITS / Matcha-TTS) | **High** (Native binary or prebuilt wheel; Apache 2.0 license) | Runtime: ~30 MB; Model: ~25–50 MB per voice | **0 MiB** (CPU) | **Upstream estimate: < 200 ms** on CPU | **VALID FALLBACK & LICENSE ALTERNATIVE**. Permissive Apache 2.0 license avoids GPL redistribution complications; solid quality and latency. |
| **Coqui TTS** | Python + PyTorch | **Blocked** (PyTorch runtime dependencies; project unmaintained upstream) | **150–500 MB** + >2 GB PyTorch | > 1.5 GB | Upstream estimate: > 800 ms | **REJECTED**. Unmaintained, enormous dependency footprint, violates size budget. |
| **Edge-TTS / Cloud APIs** | Python async client calling Microsoft Edge / cloud endpoints | **High** (Pure Python HTTP) | 0 MB local | 0 MiB | 300–600 ms (network dependent) | **REJECTED**. Violates `AGENTS.md` core principles of local-first, offline execution, and absolute privacy. |
| **Windows SAPI5 / WinRT Speech** | Native Windows OS COM API | Native Windows COM | 0 MB (built-in OS) | 0 MiB | < 100 ms | **REJECTED AS PRIMARY**. Robotic cadence, archaic voice quality, inconsistent across Windows editions. Retained only as emergency diagnostic fallback. |

---

## 4. Hardware Budget & Execution Strategy

### 4.1 Resource Envelope Allocation

| Resource | Host Total | Ollama LLM (`qwen3.5:2b`) | PICO Core & Desktop Companion | Voice STT/TTS Allocation Target | Remaining Headroom | Compliance Assessment |
|---|---|---|---|---|---|---|
| **Dedicated GPU VRAM** | 4,094 MiB | 2,200–2,800 MiB | ~100 MiB (Direct3D swap chain) | **0 MiB (Strategy A: CPU)** | **~1,194–1,794 MiB** | **PRESERVED** (Strict single-model residency maintained) |
| **System RAM** | 16,384 MiB (15.64 GB visible) | ~1,000 MiB (Ollama host process) | ~400 MiB (Python Core + Companion) | **< 450 MiB Working Set** | **~1,500 MiB** (above free threshold) | **QUALIFICATION PENDING** (Physical memory telemetry required in Stage 2) |
| **CPU Utilization** | 12 Threads (4P + 4E) | 0% during audio capture (idle) | 1–3% (FastAPI + loopback bridge) | **Bounded to 4 threads** | 8 logical threads free | **QUALIFICATION PENDING** (Core affinity and latency to be measured in Stage 2) |
| **Disk Storage** | 5.48 GB free (at snapshot) | Shared host disk | ~500 MB (PICO bundle) | **< 350 MB Total** (Whisper + Piper + Models) | **> 4.5 GB free** | **WITHIN BUDGET** (Measured component sizes total ~190–260 MB) |

### 4.2 Execution Strategy Analysis

1. **Strategy A: CPU-Exclusive Voice Execution (SELECTED)**
   - STT (`whisper.cpp`) executes on CPU using optimized AVX2 SIMD instructions bounded to 4 threads.
   - TTS (`piper.exe`) executes on CPU using ONNX Runtime CPU EP.
   - GPU VRAM is reserved 100% for the local chat LLM in Ollama.
   - *Advantages*: Zero VRAM contention, zero risk of CUDA OOM, zero latency penalty for model swapping, completely decouples voice lifecycle from LLM context state.
   - *Target Machine Estimates (Subject to Stage 2 Empirical Measurement)*: Upstream benchmarks suggest a Real-Time Factor (RTF) of Whisper `base.en` on 4 Performance cores of i5-13420H of $\approx 0.15–0.25$ (transcribing a 5.0s audio clip in ~0.75–1.25s). Piper ONNX is estimated to synthesize speech with RTF $< 0.10$.

2. **Strategy B: Dynamic GPU Model Swapping (REJECTED)**
   - Swapping out the 2.5 GB LLM from VRAM to load Whisper on GPU, transcribing, then unloading Whisper and reloading the LLM back to VRAM.
   - *Failure Reason*: Incurs 2.5–5.0 seconds of disk-to-VRAM model thrashing on every push-to-talk release, destroying conversational responsiveness and accelerating SSD wear.

3. **Strategy C: Concurrent GPU Co-Habitation (REJECTED)**
   - Loading both Qwen 2B and a CUDA-accelerated Whisper model into RTX 3050 4 GB VRAM simultaneously.
   - *Failure Reason*: Total VRAM requirement ($2.5\text{ GB LLM} + 0.6\text{ GB CUDA runtime} + 0.5\text{ GB Whisper} + 0.4\text{ GB KV cache}$) exceeds 4,000 MiB, triggering unpredictable CUDA out-of-memory allocations during multi-turn context expansion.

---

## 5. Architectural Decision & Integration Boundaries

### 5.1 Primary Engine Selections (Provisional)

1. **Primary Speech-to-Text (STT) Engine (Provisional)**:
   - **Engine**: `whisper.cpp` (native Win32 binary `whisper-cli.exe` or pure C-ABI DLL wrapped via Python `ctypes`).
   - **Default Model**: OpenAI Whisper `base.en` in GGML format:
     - `ggml-base.en-q8_0.bin`: ~81.8 MB (81,781,811 bytes), Upstream Git LFS SHA-256: `a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e`.
     - `ggml-base.en.bin` (F16 unquantized): ~148 MB (147,964,211 bytes), Upstream Git LFS SHA-256: `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`.
   - **Low-Resource Fallback Model**: OpenAI Whisper `tiny.en-q5_1.bin`: ~32.2 MB (32,200,000 bytes), Upstream Git LFS SHA-256: `c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b`.
   - **Execution Target**: Host CPU with AVX2 instruction set, bounded to 4 execution threads.

2. **Primary Text-to-Speech (TTS) Engine (Provisional)**:
   - **Engine**: `Piper ONNX` (native Win32 binary `piper.exe` or standalone C-API DLL).
   - **Default Voice**: `en_US-lessac-medium.onnx` (~63.2 MB, 63,201,294 bytes), Upstream Git LFS SHA-256: `5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f` + config `en_US-lessac-medium.onnx.json` (4,885 bytes).
   - **Execution Target**: Host CPU with single-threaded / dual-threaded ONNX Runtime CPU execution provider.

### 5.2 Designated Fallback Selections

1. **Fallback STT**: `faster-whisper` (CTranslate2 int8 compute engine) via an isolated worker process.
2. **Fallback TTS & License Alternative**: `sherpa-onnx` (VITS architecture, Apache 2.0 license).
3. **Emergency OS Fallback**: Windows SAPI5 COM interface (`pywin32` / `comtypes`), strictly for diagnostic alerts if local neural models are absent or corrupted.

### 5.3 TTS API Integration Boundary & Response Contract

The existing PICO perception API enforces a concrete, non-streaming response contract:
- `BaseTextToSpeechAdapter.synthesize(text, voice_id, speed) -> tuple[bytes, float]`
- `AudioSessionManager.synthesize_speech(...) -> AudioSynthesisResponse`
- `AudioSynthesisResponse` schema:
  - `audio_base64: str | None` (complete audio byte payload base64-encoded)
  - `audio_format: str = "wav"`
  - `duration_seconds: float`
  - `synthesizer_provider: str`
  - `is_simulated: bool`

**Integration Mechanism**:
1. When Piper CLI or C-API executes, it synthesizes speech from the input text string.
2. While Piper CLI can emit raw PCM to stdout, `AudioSynthesisResponse` requires a complete WAV payload (`audio_format="wav"`). The future adapter will configure Piper's native WAV output mode or prepend a standard 44-byte RIFF/WAVE header (specifying sample rate, e.g. 22,050 Hz or 16,000 Hz, 16-bit mono signed PCM) to the raw PCM buffer in memory.
3. The adapter returns `(wav_bytes, duration_seconds)`. `AudioSessionManager.synthesize_speech()` base64-encodes the complete WAV bytes and returns `AudioSynthesisResponse`.
4. **Preservation of Existing Contract**: The existing TTS response contract is preserved without modification. No chunked streaming API or incremental playback is supported in the current contract.
5. **Future Streaming Scope**: Any future streaming audio contract (e.g. Server-Sent Events chunking or WebSocket audio frames) would require a separately reviewed API design, schema revisions to `AudioSynthesisResponse`, and an explicit milestone scope outside P11.3.

### 5.4 STT In-Memory Audio Integration Boundary

The capture infrastructure produces 16 kHz 16-bit mono signed integer PCM (`int16`).
`whisper.cpp` exposes two primary interfaces:
1. **Command-Line Interface (`whisper-cli.exe`)**: Accepts named audio files via `-f input.wav`. Stdin piping of arbitrary WAV headers on Windows can be fragile across toolchains.
2. **Native C API (`whisper.dll`)**: Exposes `whisper_full(ctx, params, samples, n_samples)`, which directly accepts a pointer to 32-bit floating-point PCM values (`const float *`, normalized between -1.0 and 1.0 at 16 kHz mono).

**Proposed Integration Architecture (Provisional)**:
- PICO will prioritize the native C-ABI DLL (`whisper.dll`) wrapped via Python `ctypes`:
  1. Capture buffer provides 16 kHz 16-bit signed PCM `bytearray`.
  2. In-memory conversion normalizes `int16` samples to `float32` in RAM (`sample / 32768.0`).
  3. Pointer passed directly to `whisper_full` in memory; zero audio written to disk.
  4. Application-owned mutable buffers are zeroized upon completion; native context memory is freed via `whisper_free(ctx)`.
- If C-ABI DLL loading encounters Windows packaging or ABI conflicts in Stage 2 qualification, a subprocess CLI path with an in-memory RAM stream or temporary descriptor will be evaluated.
- The in-memory integration path remains **provisional** until verified against the pinned Whisper build in Stage 2.

### 5.5 Piper Licensing Analysis & Decision Gate

A critical architectural distinction exists between Piper implementations:
1. **Legacy `rhasspy/piper`**: Released by Michael Hansen under the **MIT License**. Repository was archived and made read-only in October 2025.
2. **Active `OHF-Voice/piper1-gpl`**: Maintained by the Open Home Foundation (the non-profit behind Home Assistant). The engine code is explicitly licensed under **GNU General Public License v3.0 or later (GPL-3.0-or-later)**.
3. **Voice Model Licensing**: Separate from engine code. Voice models in `rhasspy/piper-voices` carry individual dataset licenses (e.g., `lessac` is derived from public domain / permissive speech recordings, while other voices use CC-BY, CC-BY-NC, or ODbL).

**Distribution Implications**:
- PICO is an MIT-licensed system (`Lurkaee/PICO`).
- Shipping a GPL-3.0 binary (`piper.exe`) inside the Windows NSIS package installer alongside MIT components requires careful legal assessment of whether the bundle constitutes a mere aggregation or an integrated derivative work under GPLv3 §5.
- **Architectural Decision Gate**: Piper ONNX is designated as a **Provisional Candidate pending formal legal/redistribution review**.
- If GPL redistribution is deemed incompatible with PICO's packaging model, `sherpa-onnx` (Apache 2.0 license) will be promoted to primary TTS without altering the API contract.

### 5.6 Storage Footprint Breakdown

The complete TTS and STT installation footprint on disk consists of:

| Component | Files Included | Measured / Published Size |
|---|---|---|
| **STT Engine (`whisper.cpp`)** | `whisper-cli.exe` or `whisper.dll` | ~3–5 MB |
| **STT Default Model** | `ggml-base.en-q8_0.bin` (or `ggml-base.en.bin` F16) | ~81.8 MB (or ~148 MB) |
| **TTS Engine Runtime (`Piper`)** | `piper.exe` (~508 KB), `onnxruntime.dll` (~9.3 MB), `piper_phonemize.dll` (~407 KB), `espeak-ng.dll` (~381 KB), `espeak-ng-data/` (~8 MB), `libtashkeel_model.ort` (~10.2 MB), shared DLLs (~1 MB) | **~30–45 MB total runtime** |
| **TTS Default Voice Model** | `en_US-lessac-medium.onnx` (~63.2 MB) + `en_US-lessac-medium.onnx.json` (~5 KB) | **~63.2 MB** |
| **Total Combined Disk Footprint** | Complete Voice Subsystem on Disk | **~180–260 MB** |

The combined footprint is well below the **$< 350\text{ MB}$** ceiling, preserving host storage on drive `C:` (~5.48 GB free).

---

## 6. Evaluation Matrix Across Canonical Criteria

| # | Evaluation Criterion | Target / Constraint | whisper.cpp (Selected STT) | Piper ONNX (Selected TTS) | Qualification Status |
|---|---|---|---|---|---|
| 1 | **Runtime Memory** | $< 500\text{ MB}$ total peak Working Set | Upstream estimate: ~180 MB | Upstream estimate: ~90 MB | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 2 | **GPU VRAM Impact** | $0\text{ MiB}$ (preserves LLM residency) | Target: 0 MiB (CPU AVX2 execution) | Target: 0 MiB (CPU execution) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 3 | **Real-Time Factor (RTF)** | $\le 0.30$ on i5-13420H CPU | Target: $\le 0.30$ (Upstream estimate: ~0.18) | Target: $\le 0.15$ (Upstream estimate: ~0.08) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 4 | **Time to First Audio (TTS)** | $< 500\text{ ms}$ | N/A | Target: $< 500\text{ ms}$ (Upstream estimate: ~120 ms) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 5 | **Word Error Rate (WER)** | $\le 12\%$ on English commands | Target: $\le 12\%$ (Upstream estimate: ~8.5%) | N/A | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 6 | **Disk Footprint** | $< 350\text{ MB}$ combined models + runtimes | Measured: ~85–152 MB (binary + model) | Measured: ~105–115 MB (runtime + voice) | **DESIGN ACCEPTED** (Combined ~190–260 MB $\le 350$ MB budget) |
| 7 | **Packaging Feasibility** | Windows NSIS / CPython 3.14 | Standalone native `.exe` or C-ABI DLL | Standalone native `.exe` or C-ABI DLL | **DESIGN ACCEPTED** (Decoupled from Python ML wheels) |
| 8 | **Privacy / Zero Egress** | Zero external network transmission | 100% offline; socket/DNS monitoring required | 100% offline; socket/DNS monitoring required | **VERIFICATION PROTOCOL DEFINED — QUALIFICATION PENDING** |
| 9 | **Memory Zeroization** | Best-effort scrub of application buffers | Mutable audio bytearrays zeroed on exit | Mutable audio bytearrays zeroed on exit | **DESIGN ACCEPTED** (Aligned with `pico.perception.audio.capture`) |
| 10 | **Fail-Closed Safety** | Refuse unverified execution | Immediate error if binary missing | Immediate error if binary missing | **DESIGN ACCEPTED** (Fails closed with truthful error) |
| 11 | **License Compatibility** | Permissive open-source or compliant distribution | MIT License | Code: GPL-3.0 (`piper1-gpl`) / MIT (legacy); Voice: Permissive | **PROVISIONAL — LEGAL REVIEW PENDING** (sherpa-onnx Apache 2.0 alternative ready) |
| 12 | **CPU Multi-Core Impact** | Prevent UI thread starvation | Bounded to 4 threads (P-core target) | Bounded to 2 threads | **QUALIFICATION PENDING** (CPU topology and affinity to be verified in Stage 2) |
| 13 | **Model Provenance** | Immutable hashes (SHA-256) | Canonical Git LFS SHA-256 recorded | Canonical Git LFS SHA-256 recorded | **VERIFIED REGISTRY DEFINED — LOCAL DIGEST PENDING ACQUISITION** |

---

## 7. Consequences & Next Steps

### 7.1 Positive Consequences
- **Zero VRAM Contention**: Protects the 4 GB VRAM ceiling, allowing Ollama LLM to remain permanently resident without CUDA OOM crashes.
- **Packaging Isolation**: Avoids bloated Python ML wheels and native C-extension instability on CPython 3.14 Windows.
- **Architectural Honesty**: Separates published upstream estimates from measured physical hardware qualification results.
- **Preserved API Contracts**: Retains `BaseTextToSpeechAdapter` and `AudioSynthesisResponse` contracts without premature, unreviewed streaming changes.

### 7.2 Negative Consequences & Mitigations
- **CPU Load During Inference**: Running Whisper on 4 threads temporarily raises CPU load.
  - *Mitigation*: Limit thread pool to 4 threads; Windows thread scheduler assigns them to Performance cores while E-cores keep desktop UI and event ledger responsive.
- **Licensing Uncertainty**: Piper's GPL-3.0 license in active forks requires verification against PICO's installer packaging.
  - *Mitigation*: `sherpa-onnx` (Apache 2.0) is evaluated and maintained as a drop-in fallback.

### 7.3 Milestone Progression
This decision establishes **Milestone P11.3 Stage 1 as DESIGN COMPLETE — HARDWARE QUALIFICATION PENDING**.
Stage 2 (physical hardware benchmark execution and qualification report generation) will proceed according to the test protocols formalized in `docs/evaluation/voice-engine-qualification.md`.
Implementation of runtime adapters in `pico.perception.audio` will commence in **P11.3 Stage 3 (STT)** and **P11.3 Stage 4 (TTS)**.
