# Local Voice Engine Hardware Qualification Protocol (Milestone P11.3 Stage 1)

- **Document Version**: 1.1.0
- **Status**: Design Complete — Hardware Qualification Pending
- **Related ADR**: [ADR 0005: Local STT/TTS Engine Selection & Hardware Qualification](../decisions/0005-local-voice-engine-selection-and-hardware-qualification.md)
- **Governing Constraints**: `AGENTS.md` (Hardware Constraints, Single-Model Residency, Privacy, Security)

---

## 1. Overview & Objectives

Milestone P11.3 Stage 1 formalizes the provisional selection and benchmarking protocol for local Speech-to-Text (STT) and Text-to-Speech (TTS) runtimes on PICO's target hardware.

This qualification protocol establishes reproducible, verifiable benchmarks to ensure that local voice components:
1. Satisfy real-time latency thresholds for natural push-to-talk interactions.
2. Execute strictly within the target hardware envelope without starving system RAM or evicting the resident LLM from VRAM.
3. Operate in complete network isolation with verified zero outbound connection attempts, zero DNS queries, and zero raw-audio disk retention.
4. Integrate cleanly with the packaged Windows runtime (CPython 3.14.10) without incompatible C-extension dependencies.
5. Provide auditable, best-effort zeroization of application-owned audio memory upon session termination.

---

## 2. Target Hardware Baseline & Resource Boundaries

All qualification benchmarks must be measured against the following hardware specification:

| Hardware Component | Target Specification | Host Measured Baseline (Snapshot: 2026-11-15T17:31:14+05:30) | Hard Budget Boundary |
|---|---|---|---|
| **Processor** | Intel Core i5-13420H | 8 physical cores (4 Performance + 4 Efficient cores), 12 logical processors, AVX2 SIMD | Maximum 4 threads for STT inference; thread scheduling, core affinity, and UI/capture responsiveness to be empirically verified in Stage 2 |
| **System Memory** | 16 GB DDR5 | ~15.64 GB visible, **~2.35 GB free physical RAM** | Peak Voice Working Set strictly $< 450\text{ MiB}$ ($471{,}859{,}200\text{ bytes}$); Available System RAM strictly $> 1536\text{ MiB}$ ($1{,}610{,}612{,}736\text{ bytes}$) maintained under concurrent load with resident Ollama LLM to prevent OS paging |
| **Discrete GPU** | NVIDIA GeForce RTX 3050 Laptop GPU | 4,094 MiB dedicated VRAM, Driver 592.82, CUDA 13.1 | **0 MiB VRAM for Voice** (Strategy A CPU execution); preserves 100% VRAM for Ollama LLM |
| **Disk Storage** | ~60 GB available (AGENTS.md target) | **5.48 GB free on host drive `C:`** *(time-sensitive)* | Total voice binaries + DLLs + model weights $< 350\text{ MB}$; zero raw audio written to persistent disk |
| **Host OS & Shell** | Windows 11 Home x64 | Windows 11 Build 26100, PowerShell 5.1 / 7+ | Native Win32 binaries or C-ABI ctypes/cffi; no WSL requirement |
| **Packaged Python** | CPython 3.14.10 x64 | `cp314-win_amd64` isolated bundle (`--require-hashes`) | Zero dependency on multi-gigabyte Python ML wheels (e.g. PyTorch) in NSIS distribution |

---

## 3. Benchmarking Protocols & Metrics

### 3.1 Speech-to-Text (STT) Qualification Protocol

#### 3.1.1 Metric Definitions & Explicit Formulas
- **Processing Time ($T_{\text{proc}}$)**: Wall-clock duration from the moment audio PCM bytes are submitted to the STT engine until the final text transcript is emitted.
- **Audio Duration ($T_{\text{audio}}$)**: Duration of the input audio sample.
- **Real-Time Factor (RTF)**:
  $$\text{RTF} = \frac{T_{\text{proc, seconds}}}{T_{\text{audio, seconds}}} = \frac{T_{\text{proc, milliseconds}}}{T_{\text{audio, milliseconds}}}$$
  An $\text{RTF} < 1.0$ indicates faster-than-real-time processing.
  - **Passing Example**: For a 5.0-second ($5{,}000\text{ ms}$) audio clip transcribed in $1.5\text{ seconds}$ ($1{,}500\text{ ms}$):
    $$\text{RTF} = \frac{1.5\text{ s}}{5.0\text{ s}} = \frac{1{,}500\text{ ms}}{5{,}000\text{ ms}} = 0.30 \quad (\text{Complies with } \text{RTF} \le 0.30 \text{ target})$$
- **Latency Distribution**: P50 (median), P90, P95, and maximum observed latency across 10 repeated warm-state iterations per audio test clip.
- **Word Error Rate (WER)** & **Character Error Rate (CER)**:
  $$\text{WER} = \frac{S + D + I}{N}$$
  Where $S$ is substitutions, $D$ is deletions, $I$ is insertions, and $N$ is total words in the reference transcript.

#### 3.1.2 Benchmark Test Audio Corpus & Accuracy Standards (GATE-VOICE-02)

Accuracy qualification requires evaluating Word Error Rate ($\text{WER}$) across conversational English commands. Per project owner approval on 2026-11-20 adopting **Option B** ([Proposal 0006](../decisions/0006-proposal-p11-3-voice-qualification-promotion-rules.md)), the corpus structure is strictly stratified:

1. **Formal Accuracy Corpus (Option B — Mandatory for GATE-VOICE-02 Qualification)**:
   - Requires naturally spoken audio recordings of the **exact four canonical command texts**, spoken by a natural, consenting human operator using a physical microphone under standard room acoustic conditions:
     1. `audio_cmd_short_1s.wav`: `"PICO stop."` ($N=2$ words)
     2. `audio_cmd_query_3s.wav`: `"What tasks are currently waiting for my approval?"` ($N=8$ words)
     3. `audio_cmd_workflow_5s.wav`: `"Summarize the latest system logs and create a new draft task for review."` ($N=13$ words)
     4. `audio_cmd_dictation_10s.wav`: `"We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically."` ($N=26$ words)
   - **Canonical Reference Word Count Total**: $N_{\text{total}} = 2 + 8 + 13 + 26 = 49\text{ reference words}$ across the four canonical commands.
   - **Audio Format Specification**: 16 kHz sample rate, 16-bit signed integer linear PCM (little-endian), 1 channel (mono), RIFF/WAVE container.
   - **Ingestion & Provenance Requirements**: Each recording must be registered in `docs/evaluation/voice_corpus_manifest.json` with an immutable SHA-256 cryptographic digest, verified ground-truth reference transcript, consenting speaker identifier, physical microphone model, and recording environment metadata.
   - **No Demographic Quotas**: In accordance with the elimination of specification drift, no arbitrary demographic or multi-speaker minimums (e.g. 5–10 speakers across specific genders or regional accents) are required.
   - **Acceptance Threshold**: Measured aggregate $\text{WER} \le 12.0\%$ across the four canonical human command recordings ($N_{\text{total}} = 49$ reference words) is required for **`PASS`**. If measured $\text{WER} > 12.0\%$, the gate is **`FAIL`**.
   - **Current Status**: Formal human recordings have not yet been produced and registered. Until qualifying human recordings are supplied and physically benchmarked, `GATE-VOICE-02` remains **`NOT RUN`**.
   - **Non-Claims Boundary**: Passing this limited four-command corpus provides empirical evidence strictly for the four canonical command texts, the recorded speaker, the tested physical microphone, and the pinned STT model/runtime. It must **not** be described or interpreted as proof of general multi-speaker robustness, regional accent tolerance, or unconstrained conversational robustness.

2. **Automated CI Smoke-Test Fixtures (Synthetic SAPI Audio)**:
   - The four existing Windows SAPI TTS clips (`David Desktop, synthetic en-US`) corresponding to the canonical commands are retained in the repository test suite as deterministic smoke-test fixtures for automated CI runs and latency benchmarking (`GATE-VOICE-01`).
   - **Explicit Non-Qualification Boundary**: Synthetic SAPI clips **cannot satisfy formal GATE-VOICE-02 accuracy qualification**. Zero percent WER on synthetic SAPI speech does not demonstrate human speech recognition accuracy.

3. **Supplementary Real-Speech Telemetry (Informational)**:
   - Historical speech recordings (e.g. JFK 1961 inaugural address slices) and digit sequences (`vosk_digits.wav`) may be recorded as supplementary, non-blocking telemetry. They do not replace or substitute for the four canonical human command recordings.

#### 3.1.3 Execution Matrix
For each audio sample, record:
- Cold-start latency (first inference including model load/mmap into RAM).
- Warm-state latency across 10 trials.
- Peak Working Set (`WorkingSet64`) and Private Bytes (`PrivateMemorySize64`) during transcription.
- Real-Time Factor (RTF).
- Transcript accuracy (WER, CER).

### 3.2 Text-to-Speech (TTS) Qualification Protocol

#### 3.2.1 Metric Definitions & Explicit Formulas
- **Time-to-First-Audio-Chunk (TTFAC)**: Wall-clock milliseconds from text input submission to the arrival of the first complete audio chunk in memory.
- **Synthesis Duration ($T_{\text{synth}}$)**: Total wall-clock duration to synthesize the complete audio payload.
- **Synthesized Audio Duration ($T_{\text{audio\_out}}$)**: Duration of the synthesized speech output.
- **Synthesis Real-Time Factor ($\text{RTF}_{\text{synth}}$)**:
  $$\text{RTF}_{\text{synth}} = \frac{T_{\text{synth, seconds}}}{T_{\text{audio\_out, seconds}}} = \frac{T_{\text{synth, milliseconds}}}{T_{\text{audio\_out, milliseconds}}}$$
  Target: $\text{RTF}_{\text{synth}} \le 0.15$ ($\ge 6.6\times$ faster than real-time).
- **Audio Payload Conformance**: Synthesized audio must produce a valid 16-bit mono PCM payload packaged as a standard WAV byte buffer, adhering to the existing `AudioSynthesisResponse` (`audio_format="wav"`) API contract.

#### 3.2.2 Benchmark Test Prompt Corpus
1. `tts_prompt_short`: "Action confirmed." (2 words, 17 characters)
2. `tts_prompt_medium`: "I have compiled your context and prepared three tasks for your review. Would you like me to execute them?" (19 words, 107 characters)
3. `tts_prompt_technical`: "Database migration c3d4e5f6a7b8 applied successfully. Foreign keys are enforced and WAL mode is active." (14 words, 105 characters)
4. `tts_prompt_long`: "Policy A authorization requires your manual confirmation. An external tool execution has been proposed to delete expired session records from the SQLite database. Please confirm or reject this operation." (31 words, 209 characters)

---

## 4. Resource & Telemetry Monitoring Protocol

During execution of the voice qualification suite, telemetry must be recorded via host performance APIs (`Get-Process`, Windows Performance Counters, or `psutil`):

1. **System RAM**:
   - `WorkingSet64`: Physical memory currently resident in RAM.
   - `PrivateMemorySize64`: Committed memory private to the process.
   - Delta from baseline: $\Delta \text{RAM} = \text{Peak} - \text{Baseline}$.
2. **GPU & VRAM (`nvidia-smi`)**:
   - Total dedicated VRAM used before, during, and after inference.
   - Verification that $\Delta \text{VRAM} = 0\text{ MiB}$ under Strategy A (CPU execution).
3. **CPU Utilization & Core Affinity**:
   - Profile thread execution across logical processors.
   - Empirical verification that STT worker threads (bounded to 4) do not starve the Windows audio capture loop (`sounddevice`) or the companion event loop.
4. **Disk I/O**:
   - Verification of 0 write bytes to physical storage during audio transcription and speech synthesis.

---

## 5. Security, Privacy & Safety Verification

### 5.1 Zero-Egress Network Isolation Verification
To prove zero-cloud egress, the qualification harness must not rely solely on disconnecting network adapters (as an engine could attempt outbound connections, fail, and still complete).

**Verification Requirements**:
1. **Outbound Connection Attempt Monitoring**: Monitor all outbound TCP and UDP socket connection attempts across the parent Python process, `whisper-cli.exe`, `piper.exe`, and any spawned child processes throughout the inference run (e.g. via Windows Event Tracing for Windows / ETW Network Events or Sysmon Event ID 3).
2. **DNS Query Monitoring**: Monitor system DNS client activity (e.g. ETW `Microsoft-Windows-DNS-Client`) to confirm zero DNS requests are issued by voice processes.
3. **Strict Process Attribution**: Continuously log and attribute all network activity to the process tree.
4. **Pass Criteria**:
   - Monitored evidence demonstrates **exactly zero outbound connection attempts and zero DNS queries** during local transcription and synthesis.
   - An independent offline test (with physical network adapters disabled or firewall blocking all egress) must confirm that inference executes successfully without network dependence.

### 5.2 Audio Memory Zeroization Guarantee & Boundaries

In accordance with `pico.perception.audio.capture`, PICO enforces an explicit, auditable **best-effort zeroization policy**:

$$\text{Memory Zeroization Scope}$$

| Memory Category | Description | Zeroization Guarantee | Verification Method |
|---|---|---|---|
| **Application-Owned Mutable Buffers** | Primary `bytearray` buffers allocated by `pico.perception.audio.capture` and session managers | **Guaranteed Overwritten**: Actively scrubbed in-place with `0x00` (`buf[:] = b"\x00" * len(buf)`) in `finally` blocks upon session completion, error, or cancellation. | Memory inspection before buffer `.clear()` confirms zeroed bytes |
| **Adapter IPC Copies** | Mutable buffers created by STT adapter for piping to native runtime | **Guaranteed Overwritten**: In-place overwritten with `0x00` in adapter `finally` block before release. | Adapter test assertion verifying buffer scrub |
| **Temporary Subprocess Pipes** | OS anonymous pipe buffers / stdin streams | **Best-Effort**: Closed immediately upon data transmission; OS pipe buffer memory reclaimed by Windows kernel. | Confirmed pipe close upon process completion |
| **Native Runtime Internal Memory** | C/C++ internal memory allocated by `whisper.dll` / `piper.exe` | **Runtime Bound**: Internal memory managed by native runtime; allocated context explicitly freed via `whisper_free(ctx)`. | Process exit or clean C-ABI free |
| **Immutable / OS-Managed Allocations** | Immutable Python `bytes` objects, OS audio driver buffers (PortAudio), page cache | **Cannot Guarantee Scrubbing**: Userland Python cannot overwrite immutable `bytes` memory or OS driver memory pages. | Documented boundary; no false claims of "all process memory pages zeroed" |

### 5.3 Fail-Closed Fault Tolerance
- **Test 1: Missing Binary**: Invoke transcription when `whisper-cli.exe` or `whisper.dll` is absent.
  - *Expected*: Immediate deterministic exception (`VoiceEngineUnavailableError`) with HTTP 503 / RPC error. Zero hang, zero crash, zero fabricated transcripts.
- **Test 2: Corrupted Model**: Invoke engine with truncated or invalid model weight file.
  - *Expected*: Model validation fails at startup with cryptographic digest mismatch (`VoiceModelCorruptedError`).
- **Test 3: Buffer Overflow / Malformed RIFF Header**: Pass 50 MB random noise or non-16k sample rate audio.
  - *Expected*: Rejected by P11.3.1 `AudioBufferValidator` before passing to STT engine.

---

## 6. Model Provenance & Artifact Registry

Only models matching the canonical cryptographic hashes in this registry are eligible for qualification:

### 6.1 Speech-to-Text Models (`whisper.cpp` GGML)

| Model Identifier | Parameter Count | Quantization | Canonical Source Repository | Canonical Filename | Published File Size | Canonical Upstream Git LFS SHA-256 Digest | Local Verification State |
|---|---|---|---|---|---|---|---|
| `whisper-base.en-q8_0` *(Default Primary)* | 74M | Q8_0 GGML | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-base.en-q8_0.bin` | 81,781,811 bytes (~81.8 MB) | `a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e` | Upstream verified; local check pending Stage 2 |
| `whisper-base.en-f16` *(Unquantized Reference)* | 74M | F16 Standard | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-base.en.bin` | 147,964,211 bytes (~148 MB) | `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002` | Upstream verified; local check pending Stage 2 |
| `whisper-tiny.en-q5_1` *(Low-Spec Fallback)* | 39M | Q5_1 GGML | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-tiny.en-q5_1.bin` | 32,200,000 bytes (~32.2 MB) | `c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b` | Upstream verified; local check pending Stage 2 |

### 6.2 Text-to-Speech Voices (`Piper ONNX`)

| Voice Identifier | Model Architecture | Sample Rate | Canonical Source Repository | Canonical Subpath | Published File Size | Canonical Upstream Git LFS SHA-256 Digest | Local Verification State |
|---|---|---|---|---|---|---|---|
| `en_US-lessac-medium` *(Default Primary — Provisional)* | VITS ONNX | 22,050 Hz | `https://huggingface.co/rhasspy/piper-voices` | `en/en_US/lessac/medium/en_US-lessac-medium.onnx` | 63,201,294 bytes (~63.2 MB) | `5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f` | Upstream verified; local check pending Stage 2 |
| `en_US-lessac-medium.json` *(Voice Config)* | JSON Config | N/A | `https://huggingface.co/rhasspy/piper-voices` | `en/en_US/lessac/medium/en_US-lessac-medium.onnx.json` | 4,885 bytes (~4.9 KB) | MD5: `c1f2b7bddefe113f3255ff9ef234cfd3` | Upstream verified; local check pending Stage 2 |

---

## 7. Pass/Fail Qualification Matrix & Stage-Specific Promotion Gates

Per project owner decision on 2026-11-20 approving [Proposal 0006](../decisions/0006-proposal-p11-3-voice-qualification-promotion-rules.md), the monolithic ten-gate promotion structure is formally decomposed into two decoupled, sequential milestone stages: **Stage 3 (Local STT Runtime Integration)** and **Stage 4 (Local TTS Runtime Integration)**.

All original gate definitions, numerical thresholds, and severity classifications are strictly preserved, with RAM thresholds clarified to exact binary units (`MiB`) and packaging bounds to decimal megabytes (`MB`).

---

### 7.1 Stage 3 Promotion Matrix (Local Speech-to-Text Runtime Integration)

The following matrix defines the **complete Stage 3 production-activation gate set** required before PICO can be promoted to active production speech recognition (authorizing `LocalWhisperSpeechToTextAdapter.is_available() == True` at Step E).

> [!IMPORTANT]
> **Operational Lifecycle & Phased Verification (Not All Gates Are Pre-Development Blockers)**:
> This matrix constitutes the comprehensive verification criteria for **Step E production activation**, rather than a list of prerequisites that must all be satisfied prior to Stage 3 development authorization:
> - **Step B Hardware Pre-Qualification (Authorizes Step C Development)**: Physical hardware pre-qualification gates (`GATE-VOICE-02` Option B accuracy, `GATE-VOICE-04` GPU co-residency, `GATE-VOICE-05` RAM ceiling, baseline `GATE-VOICE-06` disk non-retention, and baseline `GATE-VOICE-07` zero network/DNS egress) must be satisfied and recorded to authorize **Step C development**.
> - **Step C Development**: Authorizes authoring `LocalWhisperSpeechToTextAdapter` in `pico.perception.audio.adapter` (initially disabled, `is_available() == False`, public `transcribe()` failing closed, private test seam `_transcribe_internal`).
> - **Step D Integrated Qualification**: After implementation, dedicated test harnesses exercise `_transcribe_internal` to re-verify `GATE-VOICE-06 (Integrated)` and `GATE-VOICE-07 (Integrated)`, and verify `GATE-VOICE-09 (Integrated Fail-Closed Safety)`. Because `GATE-VOICE-09` asserts behavior on the implemented adapter class, it cannot be evaluated before adapter development.
> - **Step E Production Activation**: Authorizing `is_available() == True` requires the entire gate matrix below (including Step B baseline evidence, `GATE-VOICE-01` historical pass, Step D integrated passes, `GATE-VOICE-08` interim footprint measurement, and STT component licensing).

| Gate ID | Area / Gate | Required Qualification Threshold | Severity | Verification Method | Stage 3 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-01`** | **STT Latency** | $\text{RTF} \le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Deterministic benchmark on `audio_cmd_workflow_5s.wav` | **Mandatory Precondition**. Passed historically ($\text{RTF}=0.2437$, P50=1.1992s). |
| **`GATE-VOICE-02`** | **STT Accuracy (WER)** | $\text{WER} \le 12.0\%$ on conversational English commands across Option B human corpus | **BLOCKER** | Word Error Rate evaluation across the 4 canonical human command recordings | **Mandatory Precondition**. Currently `NOT RUN`. Requires recorded human command corpus. |
| **`GATE-VOICE-04`** | **Single-Model GPU VRAM** | GPU $\Delta\text{VRAM} = 0\text{ MiB}$; single-model Ollama LLM residency preserved with zero eviction | **BLOCKER** | `nvidia-smi` continuous polling during inference with resident `qwen3.5:2b` | **Mandatory Precondition**. Currently `NOT RUN`. Requires pre-warmed Ollama LLM during test. |
| **`GATE-VOICE-05`** | **System RAM Ceiling** | Voice peak Working Set strictly $< 450\text{ MiB}$ (`471,859,200 bytes`); System Available RAM strictly $> 1536\text{ MiB}$ (`1,610,612,736 bytes`) under concurrent load | **BLOCKER** | Host OS memory sampler during simultaneous resident LLM + STT load | **Mandatory Precondition**. Currently `FAIL` (863.1 MB recorded under historical test load). |
| **`GATE-VOICE-06`** | **Disk Non-Retention & Buffer Zeroize** | Zero raw audio bytes written to persistent filesystem; mutable capture buffers zeroized | **BLOCKER** | Windows filesystem handle monitoring and kernel minifilter audit | **Dual Verification Gate**. Step B host pre-qualification baseline; Step D integrated verification on production audio path before activation. Currently `NOT RUN`. |
| **`GATE-VOICE-07`** | **Zero Network / DNS Egress** | Zero outbound socket connections and zero DNS queries during transcription | **BLOCKER** | Socket inspection and kernel ETW DNS-client activity tracing | **Dual Verification Gate**. Step B host pre-qualification baseline; Step D integrated verification on production audio path before activation. Currently `NOT RUN`. |
| **`GATE-VOICE-08`** *(Interim Measurement)* | **Packaging Bounds (STT Interim)** | Total combined voice packaging $< 350\text{ MB}$ decimal (`350,000,000 bytes`). At Stage 3: interim footprint measurement of STT slice. | **HIGH** | Directory size check in `%LOCALAPPDATA%\PICO\` | **Informational Interim Measurement**. Retains canonical `HIGH` severity. Measured prototype STT footprint is **80.92 MiB** (~84.85 MB decimal, 84,845,619 bytes; ~24% of eventual budget). |
| **`GATE-VOICE-09`** | **Fail-Closed Safety** | Deterministic typed exceptions on missing binary or corrupted model in integrated adapter | **BLOCKER** | Unit & integration tests asserting fail-closed behavior directly on implemented adapter class | **Integrated Qualification Gate**. Validated in prototype fixtures; must be re-verified on the implemented adapter before production activation (Step D). |
| **STT Component Licensing** *(Derived Prerequisite)* | **Component-Level STT License Verification** | Permissive open-source license (MIT) for Whisper runtime, shim, and GGML model | **Prerequisite** *(Non-numbered)* | Legal audit of `whisper.cpp` (MIT) and GGML weights | **Component-Level Prerequisite** derived from ADR 0005 §2.7. Prototype STT tooling is 100% MIT. Required for production activation (Step E). `GATE-VOICE-10` remains the formal full gate at Stage 4. |

---

### 7.2 Stage 4 Promotion Matrix (Local Text-to-Speech Runtime Integration)

Before promoting PICO to **Milestone P11.3 Stage 4** (authorizing production activation of `LocalTextToSpeechAdapter`), the remaining voice gates must be satisfied:

| Gate ID | Area / Gate | Required Qualification Threshold | Severity | Verification Method | Stage 4 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-03`** | **TTS Latency** | TTFAC $< 500\text{ ms}$; Synthesis $\text{RTF}_{\text{synth}} \le 0.15$ | **BLOCKER** | High-resolution timer measuring audio chunk arrival on benchmark prompt corpus | **Mandatory Precondition for TTS**. Evaluates Piper ONNX or sherpa-onnx. |
| **`GATE-VOICE-08`** *(Combined Subsystem)* | **Packaging Bounds (Combined Subsystem)** | Combined STT + TTS binaries, DLLs, and models disk footprint $< 350\text{ MB}$ total decimal (`350,000,000 bytes`) | **HIGH** | Installer staging check (`tools/packaging/stage_windows_bundle.py`) | **Release Packaging Gate**. Retains canonical `HIGH` severity. Applies to release packaging and distribution before Windows NSIS distribution. |
| **`GATE-VOICE-10`** | **License Review** | Resolution of Piper GPL-3.0 redistribution compatibility or adoption of `sherpa-onnx` (Apache 2.0) | **BLOCKER** | Architecture & Legal Review Sign-Off | **Mandatory Precondition for Full Voice System**. Formal gate resolving copyleft redistribution in NSIS bundle. |
| **Combined Subsystem Re-Verification** | **Resource, Privacy & Fail-Closed Re-Verification** | Coexistence of STT + TTS + LLM: re-verification of `GATE-VOICE-04` ($\Delta\text{VRAM}=0\text{ MiB}$, single-model LLM residency), `GATE-VOICE-05` (Combined Working Set strictly $< 450\text{ MiB}$ [`471,859,200 bytes`], System Available RAM strictly $> 1536\text{ MiB}$ [`1,610,612,736 bytes`]), `GATE-VOICE-06` (zero audio disk retention & mutable buffer zeroization for TTS), `GATE-VOICE-07` (zero TTS outbound network & DNS egress), and `GATE-VOICE-09` (TTS integrated fail-closed safety upon missing binaries, models, or corrupted configurations) | **BLOCKER** | Comprehensive multi-modal benchmark executing simultaneous STT and TTS under resident LLM load, plus unit/integration tests asserting fail-closed behavior directly on the implemented TTS adapter class | **Mandatory Precondition for Voice System Completion**. Prevents resource regression and verifies TTS fail-closed safety directly on the Stage 4 TTS implementation, rather than merely inheriting the STT prototype pass. |

---

## 8. Operational Lifecycle & Implementation Sequence

To eliminate ambiguity between project planning, adapter authoring, and physical qualification execution, the delivery of Milestone P11.3 is governed by a strict five-step operational lifecycle:

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ FIVE-STEP OPERATIONAL LIFECYCLE FOR STAGE 3 PROMOTION                                           │
├─────────────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                                 │
│   STEP A: OWNER DECISION & NORMATIVE APPROVAL (Completed)                                       │
│   └── Owner approves proposal; amendments incorporated into ADR 0005,                           │
│       Qualification Protocol, Manifest, and Implementation Plan via merged PR.                  │
│                                                                                                 │
│   STEP B: STAGE 2 HARDWARE QUALIFICATION CLOSURE (Pre-requisite for Step C)                     │
│   └── Complete hardware pre-qualification on host machine:                                      │
│       GATE-02 (Option B Human Accuracy), GATE-04 (VRAM Co-residency with LLM),                  │
│       GATE-05 (RAM under concurrent load: WS < 450 MiB, Free > 1536 MiB),                       │
│       GATE-06 (Baseline disk non-retention), GATE-07 (Baseline network egress).                 │
│       Remediate historical GATE-05 FAIL with valid empirical evidence.                          │
│                                                                                                 │
│   STEP C: STAGE 3 DEVELOPMENT AUTHORIZATION                                                     │
│   └── Author LocalWhisperSpeechToTextAdapter in pico.perception.audio.adapter.                  │
│       CRITICAL: Public adapter remains disabled (is_available() == False) and public            │
│       transcribe() fails closed. Internal pipeline structured via private test seam             │
│       (_transcribe_internal) for isolated verification. Zero runtime bypasses.                 │
│                                                                                                 │
│   STEP D: INTEGRATED ADAPTER & PRIVACY QUALIFICATION                                            │
│   └── Dedicated test-only harness exercises _transcribe_internal to verify                      │
│       GATE-06 (Disk/Zeroize) and GATE-07 (Zero Egress) during actual native execution;          │
│       asserts public transcribe() fails closed and tests GATE-09 on missing/corrupted inputs.    │
│       Public is_available() remains strictly False. Zero REST/env-var exposure.                 │
│                                                                                                 │
│   STEP E: PRODUCTION ACTIVATION                                                                 │
│   └── Require Step B baseline, Step D integrated privacy & fail-closed passes,                  │
│       all Stage 3 gates, and STT licensing before is_available() == True is enabled.            │
│                                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

1. **Step A — Owner Decision and Normative Approval**:
   - The project owner reviews and approves the stage-specific gate decomposition, corpus standard, packaging treatment, and binary RAM thresholds.
   - The approved decisions are formally incorporated into normative project documents ([ADR 0005](../decisions/0005-local-voice-engine-selection-and-hardware-qualification.md), this Protocol, and [Implementation Plan](../implementation-plan.md)) through a reviewed and merged PR.
   - **Crucial Boundary**: Proposal documents are non-normative; normative authority is established exclusively upon merging the Step A amendment PR.

2. **Step B — Stage 2 Hardware Pre-Qualification Closure**:
   - Stage 2 hardware pre-qualification prerequisites must be satisfied and recorded in `docs/evaluation/voice-qualification-results.md` before Stage 3 implementation begins.
   - The applicable hardware pre-qualification gates must satisfy their approved Stage 3 requirements on target host hardware:
     - `GATE-VOICE-02` (STT Accuracy): Measured against the approved Option B human command corpus ($\text{WER} \le 12.0\%$).
     - `GATE-VOICE-04` (Single-Model VRAM): Verified with resident Ollama LLM (`qwen3.5:2b`), proving $\Delta\text{VRAM} = 0\text{ MiB}$ and same-artifact digest continuity.
     - `GATE-VOICE-05` (RAM Ceiling): The historical `FAIL` (863.1 MB available RAM) must be remediated with valid new telemetry proving available system RAM strictly $> 1536\text{ MiB}$ (`1,610,612,736 bytes`) and peak voice Working Set strictly $< 450\text{ MiB}$ (`471,859,200 bytes`) under concurrent load.
     - `GATE-VOICE-06` (Disk Retention) & `GATE-VOICE-07` (Network Egress): Verified via elevated kernel minifilter and ETW DNS trace audits.
   - **Strict Rule**: Owner approval alone does **not** bypass Stage 2 qualification. Thresholds cannot be softened, passes cannot be inferred from idle preflights, and unmeasured audits must remain `NOT RUN`.

3. **Step C — Stage 3 Development Authorization**:
   - Only after normative amendments are merged (Step A) **and** applicable Stage 2 pre-qualification prerequisites are satisfied and recorded (Step B), implementation of `LocalWhisperSpeechToTextAdapter` in `pico.perception.audio.adapter` is authorized to begin.
   - This includes authoring the adapter class, wiring the in-memory C-ABI Whisper shim, implementing audio conversion (16 kHz 16-bit mono PCM to float32), structuring internal transcription through a private implementation method (e.g. `_transcribe_internal`), and adding deterministic unit tests.
   - **Safety Invariant**: Authorizing development does **NOT** authorize processing live production audio. During implementation, the public adapter remains strictly disabled (`is_available() == False`) and its public entry point `transcribe()` unconditionally fails closed with `ValidationError("Speech transcription runtime is unavailable")`.
   - **Architectural Seam Design**: To resolve the testability paradox (where an adapter must be verified prior to production activation without permitting unauthorized public execution), the adapter cleanly decouples the public, availability-gated entry point (`transcribe()`) from the private, internal native transcription pipeline (`_transcribe_internal(...)`).

4. **Step D — Integrated Adapter & Privacy Qualification (`GATE-VOICE-06`, `GATE-VOICE-07`, `GATE-VOICE-09`)**:
   - Step D exercises the real, implemented `LocalWhisperSpeechToTextAdapter` class directly through a dedicated, test-only qualification harness (located strictly in `tests/integration/` or `tools/bench/`), rather than relying solely on isolated prototype fixtures:
     1. **`GATE-VOICE-06` (Integrated Disk Retention & Buffer Zeroization)**: Verify that the private native pipeline (`_transcribe_internal`) writes zero raw-audio bytes to persistent storage (audited via file handle monitoring and kernel minifilter inspection) and that mutable capture buffers are deterministically zeroized.
     2. **`GATE-VOICE-07` (Integrated Network & DNS Egress Isolation)**: Verify zero prohibited outbound network connections and zero DNS queries during active transcription on the integrated native pipeline (audited via socket inspection and ETW DNS tracing).
     3. **`GATE-VOICE-09` (Integrated Fail-Closed Verification)**: Concurrently verify that the public entry point `transcribe()` unconditionally fails closed (`ValidationError("Speech transcription runtime is unavailable")`) while `is_available() == False`, and verify typed fail-closed behavior upon missing DLLs, missing/corrupted models, and malformed audio buffers directly against the implemented adapter class.
   - **Testability Invariants & Safety Boundaries**:
     1. The public `is_available()` property remains strictly `False` throughout Step D.
     2. The public `transcribe()` entry point unconditionally fails closed for all callers.
     3. The internal qualification seam (`_transcribe_internal`) is accessible solely within test code and is strictly prohibited from being exposed via REST API endpoints, capability definitions, workflow synthesis, configuration flags, or environment variable toggles.
     4. No production code path may bypass the `is_available()` check.
     5. Step E remains the sole gate authorizing production activation (`is_available() == True`).
   - **Preservation of Step B Baseline**: Step B hardware pre-qualification establishes host-level baseline compliance. Step D is an additional, mandatory integration-level safety verification on the actual production code, not a replacement for Step B.

5. **Step E — Production Activation**:
   - Flipping `LocalWhisperSpeechToTextAdapter.is_available()` from `False` to `True` is the final, consequential step under Policy A authorization.
   - Production activation is strictly prohibited until **every Stage 3 promotion condition** has been physically verified, recorded, and accepted in `docs/evaluation/voice-qualification-results.md`:
     1. `Step B Baseline Evidence Accepted`: Stage 2 hardware pre-qualification prerequisites satisfied (`GATE-VOICE-02` $\text{WER} \le 12.0\%$, `GATE-VOICE-04` $\Delta\text{VRAM} = 0\text{ MiB}$, `GATE-VOICE-05` available RAM $> 1536\text{ MiB}$, baseline `GATE-VOICE-06`, baseline `GATE-VOICE-07`).
     2. `GATE-VOICE-01`: STT Latency ($\text{RTF} \le 0.30$, 5s audio in $\le 1.5\text{s}$) — **PASS**
     3. `GATE-VOICE-06 (Integrated)`: Zero Raw-Audio Disk Retention & Buffer Zeroization on production audio pipeline — **PASS**
     4. `GATE-VOICE-07 (Integrated)`: Zero Outbound Network & DNS Egress Isolation on production audio pipeline — **PASS**
     5. `GATE-VOICE-08 (Interim)`: Interim STT Packaging Measurement ($< 350\text{ MB}$ decimal overall allowance) — **INFORMATIONAL (HIGH)**
     6. `GATE-VOICE-09 (Integrated)`: Integrated Adapter Fail-Closed Verification — **PASS**
     7. `STT Component Licensing`: Permissive MIT license verification under ADR 0005 §2.7 — **VERIFIED**
