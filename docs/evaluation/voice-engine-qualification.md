# Local Voice Engine Hardware Qualification Protocol (Milestone P11.3 Stage 1)

- **Document Version**: 1.0.0
- **Status**: Design Complete — Hardware Qualification Pending
- **Related ADR**: [ADR 0005: Local STT/TTS Engine Selection & Hardware Qualification](../decisions/0005-local-voice-engine-selection-and-hardware-qualification.md)
- **Governing Constraints**: `AGENTS.md` (Hardware Constraints, Single-Model Residency, Privacy, Security)

---

## 1. Overview & Objectives

Milestone P11.3 Stage 1 formalizes the selection and benchmarking protocol for local Speech-to-Text (STT) and Text-to-Speech (TTS) runtimes on PICO's target hardware.

This qualification protocol establishes reproducible, verifiable benchmarks to ensure that local voice components:
1. Satisfy real-time latency thresholds for natural push-to-talk interactions.
2. Execute strictly within the target hardware envelope without starving system RAM or evicting the resident LLM from VRAM.
3. Operate in complete network isolation with verified zero-cloud egress and zero raw-audio disk retention.
4. Integrate cleanly with the packaged Windows runtime (CPython 3.14.10) without incompatible C-extension dependencies.

---

## 2. Target Hardware Baseline & Resource Boundaries

All qualification benchmarks must be measured against the following hardware specification:

| Hardware Component | Target Specification | Host Measured Baseline | Hard Budget Boundary |
|---|---|---|---|
| **Processor** | Intel Core i5-13420H | 8 cores (4P + 4E), 12 logical processors, AVX2 support | Maximum 4 threads for STT inference; UI & audio capture must retain $\ge 4$ cores |
| **System Memory** | 16 GB DDR5 | ~15.64 GB visible, ~2.35 GB free physical RAM | Peak Voice Working Set $< 450\text{ MB}$; system free RAM must remain $> 1.5\text{ GB}$ |
| **Discrete GPU** | NVIDIA GeForce RTX 3050 Laptop GPU | 4,094 MiB dedicated VRAM, Driver 592.82, CUDA 13.1 | **0 MiB VRAM for Voice** (Strategy A CPU execution); preserves 100% VRAM for Ollama LLM |
| **Disk Storage** | ~60 GB available (AGENTS.md target) | **5.48 GB free on host drive `C:`** | Total voice binaries + model weights $< 350\text{ MB}$; zero raw audio written to disk |
| **Host OS & Shell** | Windows 11 Home x64 | Windows 11 Build 26100, PowerShell 5.1 / 7+ | Native Win32 binaries or C-ABI ctypes/cffi; no WSL requirement |
| **Packaged Python** | CPython 3.14.10 x64 | `cp314-win_amd64` isolated bundle (`--require-hashes`) | Zero dependency on unreleased/uncompiled Python 3.14 C-wheels (e.g. PyTorch) |

---

## 3. Benchmarking Protocols & Metrics

### 3.1 Speech-to-Text (STT) Qualification Protocol

#### 3.1.1 Metric Definitions
- **Processing Time ($T_{\text{proc}}$)**: Wall-clock duration in milliseconds from the moment audio PCM bytes are submitted to the STT engine until the final text transcript is emitted.
- **Audio Duration ($T_{\text{audio}}$)**: Duration in seconds of the input audio sample.
- **Real-Time Factor (RTF)**:
  $$\text{RTF} = \frac{T_{\text{proc}}}{T_{\text{audio}}}$$
  An $\text{RTF} < 1.0$ indicates faster-than-real-time processing. Target: $\text{RTF} \le 0.30$ (a 5.0s clip transcribed in $\le 1.5\text{s}$).
- **Latency Distribution**: P50 (median), P90, P95, and maximum observed latency across 10 repeated warm-state iterations per audio test clip.
- **Word Error Rate (WER)** & **Character Error Rate (CER)**:
  $$\text{WER} = \frac{S + D + I}{N}$$
  Where $S$ is substitutions, $D$ is deletions, $I$ is insertions, and $N$ is total words in the reference transcript.

#### 3.1.2 Benchmark Test Audio Corpus
Testing requires a deterministic corpus of 16 kHz 16-bit mono signed PCM RIFF/WAVE files:
1. `audio_cmd_short_1s.wav`: "PICO stop." (1.0s, $N=2$)
2. `audio_cmd_query_3s.wav`: "What tasks are currently waiting for my approval?" (3.2s, $N=8$)
3. `audio_cmd_workflow_5s.wav`: "Summarize the latest system logs and create a new draft task for review." (5.1s, $N=13$)
4. `audio_cmd_dictation_10s.wav`: "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically." (10.4s, $N=27$)

#### 3.1.3 Execution Matrix
For each audio sample, record:
- Cold-start latency (first inference including model load/mmap into RAM).
- Warm-state latency across 10 trials.
- Peak Working Set and Private Bytes during transcription.
- Real-Time Factor (RTF).
- Transcript accuracy (WER, CER).

### 3.2 Text-to-Speech (TTS) Qualification Protocol

#### 3.2.1 Metric Definitions
- **Time-to-First-Audio-Chunk (TTFAC)**: Wall-clock milliseconds from text input submission to the arrival of the first playable audio buffer chunk (streaming synthesis).
- **Synthesis Duration ($T_{\text{synth}}$)**: Total wall-clock milliseconds to synthesize the complete audio stream.
- **Synthesis Real-Time Factor ($\text{RTF}_{\text{synth}}$)**:
  $$\text{RTF}_{\text{synth}} = \frac{T_{\text{synth}}}{T_{\text{audio\_out}}}$$
  Target: $\text{RTF}_{\text{synth}} \le 0.15$ ($\ge 6.6\times$ real-time).
- **Streaming Audio Integrity**: Zero buffer under-runs (clicks, pops, or stutter) during continuous 16 kHz / 22.05 kHz playback.

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
   - Utilization across Performance Cores (P-cores) vs Efficient Cores (E-cores).
   - Verification that process thread priority does not preempt the Windows audio capture pump or the Tauri desktop event loop.
4. **Disk I/O**:
   - Verification of 0 write bytes to physical storage during audio transcription and speech synthesis.

---

## 5. Security, Privacy & Safety Verification

### 5.1 Zero-Egress Network Isolation Verification
- **Test**: Run qualification harness while monitoring host network connections via `netstat -ano` and socket interception.
- **Requirement**: Zero outbound TCP/UDP connections initiated by the voice engine binaries or parent Python process.
- **Pass Criteria**: All inference operations execute completely while host network adapters are disconnected or firewalled (`PASS_OFFLINE_VERIFIED`).

### 5.2 In-Memory Audio Zeroization Verification
- **Test**: Allocate audio PCM buffers, execute transcription, and verify that all mutable byte arrays (`bytearray`) are overwritten with `0x00` (`memset`) upon session exit.
- **Requirement**: No raw speech data remains in process memory pages after the session context manager closes.

### 5.3 Fail-Closed Fault Tolerance
- **Test 1: Missing Binary**: Invoke transcription when `whisper-cli.exe` or `piper.exe` is absent.
  - *Expected*: Immediate deterministic exception (`VoiceEngineUnavailableError`) with HTTP 503 / RPC error. Zero hang, zero crash, zero fabricated transcripts.
- **Test 2: Corrupted Model**: Invoke engine with truncated or invalid model weight file.
  - *Expected*: Model validation fails at startup with cryptographic digest mismatch (`VoiceModelCorruptedError`).
- **Test 3: Buffer Overflow / Malformed RIFF Header**: Pass 50 MB random noise or non-16k sample rate audio.
  - *Expected*: Rejected by P11.3.1 `AudioBufferValidator` before passing to STT engine.

---

## 6. Model Provenance & Artifact Registry

To guarantee deterministic, reproducible environments, only models matching the canonical cryptographic hashes in this registry are eligible for qualification:

### 6.1 Speech-to-Text Models (`whisper.cpp` GGML / GGUF)

| Model Identifier | Parameter Count | Quantization | Source Repository | Expected File Size | Expected SHA-256 Digest |
|---|---|---|---|---|---|
| `whisper-base.en-q8_0` *(Default Primary)* | 74M | Q8_0 GGML | `ggerganov/whisper.cpp` (HuggingFace / GitHub) | ~148 MB (155,200,000 bytes) | `f008e0687747ef45cf1397b848974e64f26046430a6aa81e5fbf41a7884c84d7` *(canonical release digest)* |
| `whisper-tiny.en-q5_0` *(Low-Spec Fallback)* | 39M | Q5_0 GGML | `ggerganov/whisper.cpp` (HuggingFace / GitHub) | ~75 MB (78,800,000 bytes) | `2c9cac54d929944f23b2075b31e5052fe2132e01fb33f7899b8ae25ea55ee2a7` *(canonical release digest)* |

### 6.2 Text-to-Speech Voices (`Piper ONNX`)

| Voice Identifier | Model Architecture | Sample Rate | Source Repository | Expected File Size | Expected SHA-256 Digest |
|---|---|---|---|---|---|
| `en_US-lessac-medium` *(Default Primary)* | VITS ONNX | 22,050 Hz | `rhasspy/piper-voices` (HuggingFace) | ~35 MB (36,700,000 bytes) + 4 KB JSON | `5e85fcfb95493019864223d6a2f3922c2a5ce459d28dbb3c79c884b23269b61d` *(canonical release digest)* |
| `en_US-amy-medium` *(Alternative Primary)* | VITS ONNX | 22,050 Hz | `rhasspy/piper-voices` (HuggingFace) | ~40 MB (42,000,000 bytes) + 4 KB JSON | `8c460cf6498ff0c96677f988c5efb0e6dfb4c73336d820ff56a73c1c7365ecae` *(canonical release digest)* |

---

## 7. Pass/Fail Qualification Matrix & Promotion Gates

Before promoting Milestone P11.3 from **Stage 1 (Design)** to **Stage 3/4 (Runtime Implementation)**, the following gates must be physically verified:

| Gate ID | Area | Required Qualification Threshold | Severity | Verification Method |
|---|---|---|---|---|
| **GATE-VOICE-01** | STT Latency | RTF $\le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Deterministic benchmark script (`tools/bench/voice_bench.py`) |
| **GATE-VOICE-02** | STT Accuracy | WER $\le 12.0\%$ on standard evaluation corpus | **BLOCKER** | Word Error Rate comparison against ground truth |
| **GATE-VOICE-03** | TTS Latency | TTFAC $< 500\text{ ms}$; Synthesis $\text{RTF} \le 0.15$ | **BLOCKER** | High-resolution timer streaming audio chunk arrival |
| **GATE-VOICE-04** | Single-Model VRAM | GPU VRAM delta $\Delta\text{VRAM} = 0\text{ MiB}$ during voice execution | **BLOCKER** | `nvidia-smi` continuous polling before/during/after |
| **GATE-VOICE-05** | RAM Ceiling | Voice peak Working Set $< 450\text{ MB}$; System Free RAM $> 1.5\text{ GB}$ | **BLOCKER** | Host OS memory telemetry during simultaneous LLM + Voice |
| **GATE-VOICE-06** | Disk Retention | Zero audio bytes written to persistent filesystem | **BLOCKER** | Windows file system handle monitoring during session |
| **GATE-VOICE-07** | Network Egress | Zero external socket connections or DNS queries | **BLOCKER** | Offline network sandbox verification |
| **GATE-VOICE-08** | Packaging Bounds | Total engine binaries + models disk footprint $< 350\text{ MB}$ | **HIGH** | Directory size check in `%LOCALAPPDATA%\PICO\` |
| **GATE-VOICE-09** | Fail-Closed Safety | Proper exception raised on missing binary or truncated model | **BLOCKER** | Unit test asserting fail-closed error without fallback mock |

---

## 8. Hardware Qualification Execution Plan (Next Steps)

1. **Stage 1 (Current)**: Architecture design and qualification protocol approval (**DESIGN COMPLETE**).
2. **Stage 2 (Benchmark Execution)**:
   - Procure verified binaries (`whisper-cli.exe`, `piper.exe`) and canonical models in an isolated developer workspace.
   - Execute the physical qualification script (`tools/bench/voice_bench.py` when implemented).
   - Publish empirical telemetry report in `docs/evaluation/voice-qualification-results.md`.
3. **Stage 3 (STT Implementation)**:
   - Implement `PicoSubprocessSTTAdapter` adhering to `pico.perception.audio.adapter`.
   - Connect live transcription to push-to-talk session completion.
4. **Stage 4 (TTS Implementation)**:
   - Implement `PicoSubprocessTTSAdapter` streaming audio chunks to host audio output.
