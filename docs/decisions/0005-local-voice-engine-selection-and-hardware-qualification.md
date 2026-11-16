# 0005. Local STT/TTS Engine Selection & Hardware Qualification

- **Date**: 2026-11-15
- **Status**: Accepted (Canonical Architecture for Milestone P11.3 Stage 1) — Design Complete; Provisional Engine Selections Pending Hardware Qualification & Legal/Licensing Review
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
   - STT Acceptance Target: Real-Time Factor $\text{RTF} \le 0.30$ on Intel Core i5-13420H CPU (transcribing a 5.0s audio clip in $\le 1.5\text{s}$, where $\text{RTF} = T_{\text{proc, seconds}} / T_{\text{audio, seconds}} = T_{\text{proc, ms}} / T_{\text{audio, ms}}$).
   - TTS Acceptance Target: Complete-WAV API Response Latency $T_{\text{response}} < 500\text{ ms}$ for short conversational confirmations on CPU; engine-internal Time-to-First-Audio-Chunk $\text{TTFAC}_{\text{internal}} < 200\text{ ms}$; synthesis Real-Time Factor $\text{RTF}_{\text{synth}} \le 0.15$. The current architecture returns complete audio buffers; user-visible first-playable audio latency ($T_{\text{first-playable}}$) is distinguished from engine-internal chunk latency.
4. **Offline Privacy & Security Invariants**: Zero cloud transmission, zero network telemetry, monitored absence of outbound connection attempts and DNS queries, auditable best-effort mutable audio buffer zeroization upon completion, zero disk retention of raw voice audio.
5. **Accuracy & Intelligibility**: STT Word Error Rate (WER) target $\le 12\%$ on conversational English commands; TTS high intelligibility with clear pronunciation of technical terminology.
6. **Storage Footprint**: Total combined disk footprint of STT binary + TTS binary + shared DLLs + voice models must remain strictly below the **$< 350\text{ MB}$** design ceiling to respect host disk space (~5.48 GB free). The architecture must distinguish published upstream model sizes, published release asset sizes, estimated dependency footprints, and estimated combined installation footprints (~115–267 MB) from physically measured installed directory sizes (to be verified in Stage 2).
7. **Licensing & Redistribution Compatibility**: Rigorously separate and independently evaluate:
   - **Engine Code License**: Permissive (MIT / Apache 2.0) vs copyleft (GPL-3.0-or-later).
   - **Voice-Model Dataset License**: Rights granted by the underlying training corpus.
   - **Voice-Model Redistribution Rights**: Whether trained weights may be redistributed with PICO.
   - **Voice-Model Training Lineage**: Whether a voice was trained from scratch on public data or fine-tuned from a restricted base model.
   - Non-negotiable boundary: `en_US-lessac-medium` is NOT CLEARED FOR DEFAULT PICO DISTRIBUTION due to the Lessac Blizzard 2013 non-commercial research restriction. A replacement voice with a verified clean dataset license chain (e.g. Public Domain `en_US-ljspeech-medium`) must be evaluated. The engine distribution gate must remain strictly decoupled from the voice-model distribution gate.

---

## 3. Evaluated Candidates

### 3.1 Speech-to-Text (STT) Candidates

| Engine | Architecture / Runtime | Windows Packaging Compatibility | Published Model Footprint | Typical VRAM Footprint | Published / Estimated CPU RTF | Assessment |
|---|---|---|---|---|---|---|
| **whisper.cpp** *(Selected Primary — Provisional)* | Native C/C++ (GGML/GGUF), standalone CLI / C-ABI DLL, AVX2 SIMD | **100% Decoupled** (Native Win32 executable or C-ABI ctypes/cffi; zero Python ML wheel dependencies) | **32.2 MB** (`tiny.en-q5_1.bin`) to **81.8 MB** (`base.en-q8_0.bin`) / **148 MB** (`base.en.bin` F16); runtime binary: ~3–5 MB | **0 MiB** (CPU mode); ~250–400 MiB (optional cuBLAS) | **Upstream estimate: ~0.15–0.25** (`base.en` on 4 CPU threads); *qualification pending* | **RECOMMENDED PRIMARY (Provisional)**. Fast, self-contained, minimal disk footprint, zero VRAM contention with resident LLM. Pinned upstream release `v1.7.4` (commit `8a9ad7844d6e2a10cddf4b92de4089d7ac2b14a9`) / `v1.9.5` (commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`), HF repo commit `5359861c739e955e79d9a303bcbc70fb988958b1`. In-memory C-ABI vs CLI integration to be verified in Stage 2. |
| **faster-whisper** *(Designated Fallback)* | CTranslate2 + HuggingFace tokenizers (Python + C++ backend) | **Supported on Windows cp314** (`ctranslate2-*-cp314-cp314-win_amd64.whl` exists), but requires bundling `tokenizers`, `av`, and C++ runtime DLLs | **145 MB** (`base.en` int8) | **0 MiB** (CPU); ~450 MiB (CUDA) | **Upstream estimate: ~0.18–0.28** (`base.en` CPU) | **VALID FALLBACK**. Highly optimized int8 compute, but introduces a larger dependency graph into the Python environment. |
| **openai-whisper (PyTorch)** | Python + PyTorch (`torch`, `torchaudio`) | **Blocked by Size** (PyTorch runtime wheels > 2 GB for CUDA / > 200 MB for CPU; massive installation footprint) | ~140 MB model + **> 2.0 GB** PyTorch runtime | > 1.8 GB (PyTorch CUDA runtime overhead) | **Upstream estimate: ~0.60–1.10** | **REJECTED**. Disproportionate storage and memory overhead; violates single-model residency and packaging constraints. |
| **sherpa-onnx (Offline STT)** | C++ / ONNX Runtime (Whisper / Zipformer / Conformer) | **High** (Native binary or prebuilt wheels via Apache 2.0) | **80–200 MB** (ONNX quantized) | **0 MiB** (CPU); ~350 MiB (DirectML/CUDA) | **Upstream estimate: ~0.20–0.30** | **VIABLE ALTERNATIVE**. Permissive Apache 2.0 engine code; retained as candidate if GGML packaging encounters blockers. Note: Individual acoustic models carry independent dataset licenses that require separate verification. |

### 3.2 Text-to-Speech (TTS) Candidates

| Engine | Architecture / Runtime | Windows Packaging Compatibility | Published Runtime & Model Footprint | Typical VRAM Footprint | Published / Estimated Latency | Assessment |
|---|---|---|---|---|---|---|
| **Piper ONNX** *(Selected Primary — Provisional)* | C++ / ONNX Runtime, standalone `piper.exe` or C-FFI library | **100% Decoupled** (Standalone native binary invoked via pipe or C-API; zero Python ML wheels) | Runtime asset: ~22.5 MB zip expanding to ~30–45 MB; Model: ~63.2 MB (`lessac-medium`) or ~63.5 MB (`ljspeech-medium`). Total estimated: **~95–115 MB** | **0 MiB** (CPU-exclusive execution) | **Upstream estimate: < 150 ms** internal chunk arrival on modern x86 CPU; *qualification pending* | **RECOMMENDED PRIMARY (Provisional)**. High naturalness, instant synthesis on CPU, zero VRAM impact. Engine code is MIT in legacy `rhasspy/piper` (commit `38917ffd8c0e219c6581d73e07b30ef1d572fce1`) and GPL-3.0 in active `OHF-Voice/piper1-gpl` (commit `639388b6317fc4731e91d53da42aea68fd4166ff`). `lessac` voice model is NOT CLEARED FOR DISTRIBUTION due to Blizzard 2013 research-only dataset license; clean Public Domain candidate `ljspeech-medium` evaluated. |
| **sherpa-onnx (VITS)** *(Designated Fallback)* | C++ / ONNX Runtime (VITS / Matcha-TTS) | **High** (Native binary or prebuilt wheel; Apache 2.0 license) | Runtime: ~30 MB; Model: ~25–50 MB per voice | **0 MiB** (CPU) | **Upstream estimate: < 200 ms** on CPU | **VALID FALLBACK & LICENSE ALTERNATIVE**. Permissive Apache 2.0 engine license avoids GPL redistribution complications; solid quality and latency. Caveat: Upstream voice models must be independently vetted for dataset redistribution rights (Apache 2.0 engine license does not automatically clear associated models). |
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
| **CPU Utilization** | 12 Logical Processors (4P + 4E physical) | 0% during audio capture (idle) | 1–3% (FastAPI + loopback bridge) | **Bounded to 4 execution threads** | 8 logical processors free | **QUALIFICATION PENDING** (Core affinity, P/E scheduling, and latency to be measured in Stage 2) |
| **Disk Storage** | 5.48 GB free (at snapshot) | Shared host disk | ~500 MB (PICO bundle) | **< 350 MB Total Design Ceiling** (Whisper + Piper + Models) | **> 4.5 GB free** | **DESIGN WITHIN BUDGET** (Estimated components total ~115–267 MB; physical measurement pending Stage 2) |

### 4.2 Execution Strategy Analysis

1. **Strategy A: CPU-Exclusive Voice Execution (SELECTED)**
   - STT (`whisper.cpp`) executes on CPU using optimized AVX2 SIMD instructions bounded to a provisional maximum of 4 threads.
   - TTS (`piper.exe`) executes on CPU using ONNX Runtime CPU EP.
   - GPU VRAM is reserved 100% for the local chat LLM in Ollama.
   - *Advantages*: Zero VRAM contention, zero risk of CUDA OOM, zero latency penalty for model swapping, completely decouples voice lifecycle from LLM context state.
   - *Target Machine Estimates (Subject to Stage 2 Empirical Measurement)*: Upstream benchmarks suggest a Real-Time Factor (RTF) of Whisper `base.en` on 4 CPU threads of i5-13420H of $\approx 0.15–0.25$ (transcribing a 5.0s audio clip in ~0.75–1.25s). Piper ONNX is estimated to synthesize speech with synthesis RTF $< 0.10$.
   - *CPU Scheduling & Affinity Boundary*: CPU logical processor affinity has not yet been mapped to physical Performance core (P-core) or Efficient core (E-core) masks. Windows thread scheduling behavior across heterogeneous Alder Lake / Raptor Lake cores is not guaranteed without explicit affinity configuration or QoS APIs. Therefore, the 4-thread execution limit is a provisional budget to bound overall CPU utilization; no claim of automatic P-core scheduling compliance is made. Actual affinity, thread distribution, and audio/UI responsiveness must be empirically established during Stage 2 qualification.

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
   - **Pinned Upstream Provenance**:
     - Pinned release tags: `v1.7.4` (resolved commit `8a9ad7844d6e2a10cddf4b92de4089d7ac2b14a9`) / `v1.9.5` (resolved commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`).
     - Upstream HuggingFace model repository: `https://huggingface.co/ggerganov/whisper.cpp` (pinned Git revision `5359861c739e955e79d9a303bcbc70fb988958b1`).
     - Build configuration: Native Windows x64 MSVC, AVX2 SIMD enabled, static OpenMP / C++ runtime, zero CUDA runtime overhead.
   - **Default Model**: OpenAI Whisper `base.en` in GGML format:
     - `ggml-base.en-q8_0.bin`: Published size 81,781,811 bytes (~81.8 MB), Upstream Git LFS SHA-256: `a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e`.
     - `ggml-base.en.bin` (F16 unquantized reference): Published size 147,964,211 bytes (~148 MB), Upstream Git LFS SHA-256: `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002`.
   - **Low-Resource Fallback Model**: OpenAI Whisper `tiny.en-q5_1.bin`: Published size 32,200,000 bytes (~32.2 MB), Upstream Git LFS SHA-256: `c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b`.
   - **Execution Target**: Host CPU with AVX2 instruction set, provisionally bounded to 4 execution threads.

2. **Primary Text-to-Speech (TTS) Engine (Provisional)**:
   - **Engine**: `Piper ONNX` (standalone native Win32 distribution or C-API library).
   - **Pinned Upstream Engine Provenance**:
     - MIT Legacy Distribution: `rhasspy/piper` release `2023.11.14-2` (resolved commit `38917ffd8c0e219c6581d73e07b30ef1d572fce1`), standalone Windows x64 release asset `piper_windows_amd64.zip` (published size 22,477,236 bytes).
     - GPL-3.0 Maintained Distribution: `OHF-Voice/piper1-gpl` tag `v1.8.0` (resolved commit `639388b6317fc4731e91d53da42aea68fd4166ff`).
     - Upstream HuggingFace voice repository: `https://huggingface.co/rhasspy/piper-voices` (pinned Git revision `e9ab671f2a0e50a701b34361136ba3e6a3a9eb0c`).
   - **Provisional Research Benchmark Voice**:
     - `en_US-lessac-medium.onnx`: Published size 63,201,294 bytes (~63.2 MB), Upstream Git LFS SHA-256: `5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f`.
     - Config `en_US-lessac-medium.onnx.json`: Published size 4,885 bytes, Upstream SHA-256: `efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0`.
     - **Distribution Status**: **NOT CLEARED FOR DEFAULT PICO DISTRIBUTION**. The Lessac Blizzard 2013 dataset license restricts use to non-commercial research and scientific evaluation and excludes commercial exploitation of resulting synthesis. Retained strictly as a provisional technical benchmark candidate where its license permits research activity.
   - **Candidate Permissive Distribution Voice**:
     - `en_US-ljspeech-medium.onnx`: Published size 63,531,379 bytes (~63.5 MB), Upstream Git LFS SHA-256: `6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22`.
     - Config `en_US-ljspeech-medium.onnx.json`: Published size 4,972 bytes, Upstream SHA-256: `141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860`.
     - **Distribution Status**: Evaluated candidate with a documented Public Domain training lineage (trained from scratch for 1000 epochs on the public domain LJ Speech dataset; no dependency on restricted base models).
   - **Execution Target**: Host CPU with single-threaded or dual-threaded ONNX Runtime CPU execution provider.

### 5.2 Designated Fallback Selections

1. **Fallback STT**: `faster-whisper` (CTranslate2 int8 compute engine) via an isolated worker process.
2. **Fallback TTS & License Alternative**: `sherpa-onnx` (VITS / Matcha-TTS architecture, Apache 2.0 engine license). Caveat: Upstream voice models must be individually vetted for dataset redistribution rights (the Apache 2.0 license of sherpa-onnx applies to engine code, not necessarily to third-party trained voice weights).
3. **Emergency OS Fallback**: Windows SAPI5 COM interface (`pywin32` / `comtypes`), strictly for diagnostic alerts if local neural models are absent or corrupted.

### 5.3 TTS API Integration Boundary & Response Contract

The existing PICO perception API enforces a concrete, non-streaming complete-buffer response contract:
- `BaseTextToSpeechAdapter.synthesize(text, voice_id, speed) -> tuple[bytes, float]`
- `AudioSessionManager.synthesize_speech(...) -> AudioSynthesisResponse`
- `AudioSynthesisResponse` schema:
  - `audio_base64: str | None` (complete audio byte payload base64-encoded)
  - `audio_format: str = "wav"`
  - `duration_seconds: float`
  - `synthesizer_provider: str`
  - `is_simulated: bool`

**Upstream Audio Generation & Adapter Conversion Path**:
1. **Engine Output Generation**: The upstream `libpiper` C API emits 32-bit floating-point audio samples (`const float *samples`, normalized to $[-1.0, 1.0]$) in chunks via `piper_audio_chunk` callbacks at the voice's configured sample rate (e.g. 22,050 Hz for `medium` voices, 1 channel mono).
2. **Buffer Accumulation**: The future adapter consumes and accumulates these float32 audio chunks in bounded application-owned memory (with an explicit maximum allocation ceiling, e.g. 10 MB, preventing unbounded memory growth).
3. **Conversion to Complete WAV Representation**:
   - **Primary Standard Path (Signed 16-Bit Linear PCM Little-Endian)**:
     - Each float32 sample is clamped to $[-1.0, 1.0]$, scaled by 32,767.0, and converted to signed 16-bit little-endian integer (`int16_t`, 2 bytes per sample).
     - Construct a standard 44-byte canonical RIFF/WAVE header:
       - `ChunkID = "RIFF"`, `Format = "WAVE"`, `Subchunk1ID = "fmt "`
       - `Subchunk1Size = 16` (PCM format chunk)
       - `AudioFormat = 1` (`WAVE_FORMAT_PCM`)
       - `NumChannels = 1` (Mono)
       - `SampleRate = sample_rate` (e.g., 22,050 Hz)
       - `BitsPerSample = 16`
       - `ByteRate = SampleRate * NumChannels * (BitsPerSample / 8)` (e.g. 44,100 bytes/sec)
       - `BlockAlign = NumChannels * (BitsPerSample / 8)` (2 bytes)
       - `Subchunk2ID = "data"`
       - `Subchunk2Size = num_samples * BlockAlign`
       - `ChunkSize = 36 + Subchunk2Size`
     - Signed 16-bit PCM little-endian is designated as the primary format because it guarantees universal playback compatibility across desktop web browsers, Tauri WebViews, and Web Audio API contexts without floating-point decoding quirks.
   - **Alternative Path (IEEE-Float WAV)**:
     - Retain raw float32 samples directly (4 bytes per sample, IEEE 754 float little-endian).
     - Construct an IEEE-float RIFF/WAVE header with `AudioFormat = 3` (`WAVE_FORMAT_IEEE_FLOAT`), `BitsPerSample = 32`, `BlockAlign = 4`, `ByteRate = SampleRate * 4`, `Subchunk2Size = num_samples * 4`, and `ChunkSize = 36 + Subchunk2Size`.
4. **Header and Length Validation**:
   - Validate that the total payload length equals `44 + Subchunk2Size`.
   - Validate chunk size fields against buffer lengths (`ChunkSize == len(wav_bytes) - 8`, `Subchunk2Size == len(pcm_bytes)`).
5. **Contract Delivery**:
   - The adapter returns `(wav_bytes, duration_seconds)` where `duration_seconds = num_samples / sample_rate`.
   - `AudioSessionManager.synthesize_speech()` base64-encodes the complete WAV bytes and returns `AudioSynthesisResponse(audio_base64=..., audio_format="wav", ...)`.
   - **Preservation of Existing Contract**: The existing TTS response contract is preserved without modification. Zero streaming APIs or chunked responses are introduced.

**Latency Distinctions & TTFAC Definition Correction**:
The existing PICO response is complete-buffer based and does not deliver the first audio chunk to the client before synthesis completes. Therefore, three distinct latency metrics must be rigorously separated:
1. **Engine-Internal Time-to-First-Audio-Chunk ($\text{TTFAC}_{\text{internal}}$)**: Wall-clock duration from text submission until `libpiper` emits the first `piper_audio_chunk` callback to the adapter. (Upstream estimate: ~120 ms). This measures engine initialization and first inference latency.
2. **API Complete-WAV Response Latency ($T_{\text{response}}$)**: Wall-clock duration from text submission until the adapter accumulates all audio chunks, converts them to PCM, constructs the WAV header, and `AudioSessionManager` base64-encodes the complete payload into `AudioSynthesisResponse`.
3. **User-Visible First-Playable-Audio Latency ($T_{\text{first-playable}}$)**: Wall-clock duration from user request until the client receives the complete payload, decodes the audio, and begins speaker playback ($T_{\text{first-playable}} \ge T_{\text{response}} + T_{\text{transport}} + T_{\text{decode}}$).
- **Rule**: Internal Piper chunk latency must NOT be labeled as user-visible TTFAC.
- The $< 500\text{ ms}$ qualification target applies to the complete-WAV response latency ($T_{\text{response}} < 500\text{ ms}$) for short conversational confirmations (e.g., "Action confirmed.").
- If true user-visible first-audio streaming is desired in the future, it would require a separate streaming transport (e.g. WebSocket audio frames or chunked SSE transfer) and client-side streaming playback, which is documented as a future architectural enhancement outside Milestone P11.3 Stage 1.

### 5.4 STT In-Memory Audio Integration Boundary & Native C API ABI Strategy

The capture infrastructure produces 16 kHz 16-bit mono signed integer PCM (`int16`).
`whisper.cpp` exposes two primary interfaces:
1. **Command-Line Interface (`whisper-cli.exe`)**: Accepts named audio files via `-f input.wav`. Stdin piping of arbitrary WAV headers on Windows can be fragile across toolchains.
2. **Native C API (`whisper.dll`)**: Exposes `whisper_full(ctx, params, samples, n_samples)`, which directly accepts a pointer to 32-bit floating-point PCM values (`const float *`, normalized between -1.0 and 1.0 at 16 kHz mono).

**ABI Compatibility & Native Binding Analysis**:
The native function signature:
`int whisper_full(struct whisper_context * ctx, struct whisper_full_params params, const float * samples, int n_samples);`
receives the `whisper_full_params` structure **by value**. This structure contains over 50 fields (integers, booleans, floating-point thresholds, and function pointer callbacks).
- **ABI Fragility**: Binding this large structure directly via Python `ctypes.Structure` is fragile. Any divergence in compiler struct alignment, field padding, data types, or upstream additions/removals across `whisper.cpp` versions causes silent memory corruption, stack smashing, or access violations.
- **Architectural Rules**:
  1. The `whisper.cpp` library and headers must be pinned to an exact release commit and build configuration (Windows x64 MSVC).
  2. The Python ctypes loader must verify exported symbols, header version markers, and expected structure sizes upon initialization, failing closed (`VoiceEngineABIMismatchError`) if any signature or size mismatch is detected.
  3. **Recommended C Shim Pattern**: To eliminate version-sensitive struct-by-value binding in Python, Stage 2 will evaluate authoring a thin, project-owned native C shim (`pico_whisper_shim.dll`). The shim compiles against the pinned `whisper.h` and exposes an opaque-context, flat C-ABI signature:
     ```c
     int pico_whisper_transcribe(
         struct whisper_context* ctx,
         const float* samples,
         int n_samples,
         int n_threads,
         char* out_text,
         int max_out_len
     );
     ```
     This confines struct layout management to the native compiler and presents a robust, minimal C-ABI boundary to Python ctypes.
  4. The in-memory integration path remains **provisional** until verified against the pinned Windows build in Stage 2.

### 5.5 Piper Engine & Voice-Model Licensing Analysis

A complete licensing assessment requires separating four distinct legal dimensions:
1. **Engine Code License**:
   - Legacy `rhasspy/piper`: Released under the **MIT License**. Repository archived and read-only since October 2025.
   - Active `OHF-Voice/piper1-gpl`: Maintained by the Open Home Foundation under **GNU General Public License v3.0 or later (GPL-3.0-or-later)**.
2. **Voice-Model Dataset License**: Rights granted by the owners of the underlying speech recording datasets.
3. **Voice-Model Redistribution Rights**: Rights to package and redistribute trained model weights with PICO. An MIT or permissive license on the engine or repository does NOT override restrictive terms in the original training corpus.
4. **Voice-Model Training Lineage**: Whether model weights were trained from scratch on public data or fine-tuned from another model. Models fine-tuned from restricted base models inherit the restrictions of the ancestor model (e.g. `libritts_r` fine-tuned from `lessac`).

**Voice Model Evaluation**:
- `en_US-lessac-medium`: Trained on the Lessac Blizzard Challenge 2013 dataset. The Blizzard Challenge license restricts use to non-commercial research and exploration and excludes commercial use of resulting synthesis. Therefore, `en_US-lessac-medium` is **NOT CLEARED FOR DEFAULT PICO DISTRIBUTION** pending explicit rights assessment. It is retained solely as a provisional technical benchmark candidate where its license permits research activity.
- `en_US-ljspeech-medium`: Trained from scratch for 1,000 epochs on the public domain LJ Speech dataset (LibriVox public domain recordings read by Linda Johnson). Carries a verified clean training lineage with public domain licensing.
- `sherpa-onnx` Permissive Alternative: Engine code is licensed under Apache 2.0. However, sherpa-onnx voice models also require individual dataset licensing audits prior to distribution (the Apache 2.0 engine license does not automatically make all associated models distributable).

**Decision Gates**:
- The **Engine Distribution Gate** (evaluating GPLv3 aggregation vs MIT distribution vs Apache 2.0 sherpa-onnx) is kept strictly decoupled from the **Voice Model Distribution Gate** (evaluating dataset license chains and commercial redistribution clearance).
- Both gates must receive formal review and sign-off before Stage 3/4 runtime packaging.

### 5.6 Storage Footprint Breakdown & Terminology

To maintain architectural precision, storage footprint analysis distinguishes:
- **Published Upstream Model Size**: Size of model files as published in upstream repositories.
- **Published Release Asset Size**: Size of compressed binary release archives.
- **Estimated Dependency/Runtime Footprint**: Uncompressed disk size of executable binaries, native DLLs, and support data.
- **Estimated Combined Installation Footprint**: Sum of estimated runtimes and published models.
- **Physically Measured Installed Footprint**: Actual on-disk byte size measured via filesystem inspection after installation (established during Stage 2).

| Component | Files Included | Published Model / Asset Size | Estimated Installed Footprint | Verification Status |
|---|---|---|---|---|
| **STT Engine (`whisper.cpp`)** | `whisper-cli.exe` or `whisper.dll` (+ thin shim) | Upstream binary asset | ~3–5 MB | Pinned upstream; measurement pending Stage 2 |
| **STT Default Model** | `ggml-base.en-q8_0.bin` (or `tiny.en-q5_1.bin`) | Published: 81.8 MB (or 32.2 MB) | ~81.8 MB (or ~32.2 MB) | Upstream Git LFS digest verified |
| **TTS Engine Runtime (`Piper`)** | `piper.exe`, `onnxruntime.dll`, `piper_phonemize.dll`, `espeak-ng.dll`, `espeak-ng-data/`, `libtashkeel_model.ort`, shared DLLs | Published archive: ~22.5 MB (`piper_windows_amd64.zip`) | ~30–45 MB total uncompressed runtime | Pinned release asset; measurement pending Stage 2 |
| **TTS Voice Model** | `en_US-ljspeech-medium.onnx` (~63.5 MB) or `en_US-lessac-medium.onnx` (~63.2 MB) + JSON (~5 KB) | Published: ~63.2–63.5 MB | ~63.5 MB | Upstream Git LFS digest verified |
| **Estimated Combined Voice Footprint** | Complete STT + TTS Subsystem on Disk | Published / Estimated | **~180–267 MB** (with base.en-q8_0) / **~125–145 MB** (with tiny.en-q5_1) | **DESIGN CEILING SATISFIED** ($< 350\text{ MB}$; physical measurement pending Stage 2) |

---

## 6. Evaluation Matrix Across Canonical Criteria

| # | Evaluation Criterion | Target / Constraint | whisper.cpp (Selected STT) | Piper ONNX (Selected TTS) | Qualification Status |
|---|---|---|---|---|---|
| 1 | **Runtime Memory** | $< 500\text{ MB}$ total peak Working Set | Upstream estimate: ~180 MB | Upstream estimate: ~90 MB | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 2 | **GPU VRAM Impact** | $0\text{ MiB}$ (preserves LLM residency) | Target: 0 MiB (CPU AVX2 execution) | Target: 0 MiB (CPU execution) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 3 | **Real-Time Factor (RTF)** | $\le 0.30$ on i5-13420H CPU | Target: $\le 0.30$ (Upstream estimate: ~0.18) | Target: $\le 0.15$ (Upstream estimate: ~0.08) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 4 | **Time to First Audio (TTS)** | Complete-WAV $T_{\text{response}} < 500\text{ ms}$; $\text{TTFAC}_{\text{internal}} < 200\text{ ms}$ | N/A | Target: $T_{\text{response}} < 500\text{ ms}$ (Upstream chunk estimate: ~120 ms) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 5 | **Word Error Rate (WER)** | $\le 12\%$ on English commands | Target: $\le 12\%$ (Upstream estimate: ~8.5%) | N/A | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| 6 | **Disk Footprint** | $< 350\text{ MB}$ combined models + runtimes | Estimated: ~35–153 MB (binary + model) | Estimated: ~95–115 MB (runtime + voice) | **DESIGN WITHIN BUDGET — PHYSICAL MEASUREMENT PENDING STAGE 2** (Estimated ~125–267 MB $\le 350$ MB budget) |
| 7 | **Packaging Feasibility** | Windows NSIS / CPython 3.14 | Standalone native `.exe` or C-ABI DLL | Standalone native `.exe` or C-ABI DLL | **DESIGN ACCEPTED** (Decoupled from Python ML wheels) |
| 8 | **Privacy / Zero Egress** | Zero external network transmission | 100% offline; socket/DNS monitoring required | 100% offline; socket/DNS monitoring required | **VERIFICATION PROTOCOL DEFINED — QUALIFICATION PENDING** |
| 9 | **Memory Zeroization** | Best-effort scrub of application buffers | Mutable audio bytearrays zeroed on exit | Mutable audio bytearrays zeroed on exit | **DESIGN ACCEPTED** (Aligned with `pico.perception.audio.capture`) |
| 10 | **Fail-Closed Safety** | Refuse unverified execution | Immediate error if binary missing | Immediate error if binary missing | **DESIGN ACCEPTED** (Fails closed with truthful error) |
| 11 | **License Compatibility** | Permissive open-source or compliant distribution | MIT License | Engine: GPL-3.0 (`piper1-gpl`) / MIT (`rhasspy/piper`); Voice: `lessac` NOT CLEARED FOR DISTRIBUTION (research only); `ljspeech` candidate (Public Domain) | **PROVISIONAL — LEGAL & REDISTRIBUTION REVIEW PENDING** (sherpa-onnx Apache 2.0 alternative ready) |
| 12 | **CPU Multi-Core Impact** | Prevent UI thread starvation | Bounded to 4 threads | Bounded to 2 threads | **QUALIFICATION PENDING** (Heterogeneous P/E Core Scheduling & Affinity Unmeasured) |
| 13 | **Model Provenance** | Immutable hashes (SHA-256) | Canonical Git LFS SHA-256 pinned | Canonical Git LFS SHA-256 pinned | **IMMUTABLE PROVENANCE PINNED — LOCAL DIGEST PENDING STAGE 2 PROCUREMENT** |

---

## 7. Consequences & Next Steps

### 7.1 Positive Consequences
- **Zero VRAM Contention**: Protects the 4 GB VRAM ceiling, allowing Ollama LLM to remain permanently resident without CUDA OOM crashes.
- **Packaging Isolation**: Avoids bloated Python ML wheels and native C-extension instability on CPython 3.14 Windows.
- **Architectural Honesty**: Strictly separates published upstream estimates from empirical hardware qualification results.
- **Preserved API Contracts**: Retains `BaseTextToSpeechAdapter` and `AudioSynthesisResponse` contracts without premature, unreviewed streaming changes.
- **Independent Distribution Gates**: Decouples engine code licensing from voice dataset licensing, ensuring PICO is never locked into restricted speech datasets.

### 7.2 Negative Consequences & Mitigations
- **CPU Load During Inference**: Running Whisper on 4 threads temporarily raises CPU load.
  - *Mitigation*: Limit STT inference thread pool to a provisional budget of 4 threads. Stage 2 must profile Windows thread scheduling, measure processor affinity against physical P-cores and E-cores, and empirically verify that audio capture and UI event loops remain unstarved under concurrent load before making scheduling compliance claims.
- **Licensing Uncertainty**: Piper's GPL-3.0 license in active forks requires verification against PICO's installer packaging, and voice datasets require clean redistribution rights.
  - *Mitigation*: `sherpa-onnx` (Apache 2.0) is evaluated and maintained as an engine fallback; public domain voices (e.g. `en_US-ljspeech-medium`) are evaluated to replace research-restricted datasets.

### 7.3 Milestone Progression
This decision establishes **Milestone P11.3 Stage 1 as DESIGN COMPLETE — HARDWARE QUALIFICATION PENDING**.
Stage 2 (physical hardware benchmark execution and qualification report generation) will proceed according to the test protocols formalized in `docs/evaluation/voice-engine-qualification.md`.
Note: The qualification harness `tools/bench/voice_bench.py` is a **planned Stage 2 deliverable** (not currently present in the repository) and will be developed and executed during Stage 2.
Implementation of runtime adapters in `pico.perception.audio` will commence in **P11.3 Stage 3 (STT)** and **P11.3 Stage 4 (TTS)**.
