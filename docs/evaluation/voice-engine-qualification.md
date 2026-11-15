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
| **System Memory** | 16 GB DDR5 | ~15.64 GB visible, **~2.35 GB free physical RAM** | Peak Voice Working Set $< 450\text{ MB}$; free system RAM must remain $> 1.5\text{ GB}$ to prevent OS paging |
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

## 7. Pass/Fail Qualification Matrix & Promotion Gates

Before promoting Milestone P11.3 from **Stage 1 (Design)** to **Stage 3/4 (Runtime Implementation)**, the following gates must be physically verified:

| Gate ID | Area | Required Qualification Threshold | Severity | Verification Method | Current Stage 1 Status |
|---|---|---|---|---|---|
| **GATE-VOICE-01** | STT Latency | RTF $\le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Deterministic benchmark script (`tools/bench/voice_bench.py`) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-02** | STT Accuracy | WER $\le 12.0\%$ on standard evaluation corpus | **BLOCKER** | Word Error Rate comparison against ground truth | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-03** | TTS Latency | TTFAC $< 500\text{ ms}$; Synthesis $\text{RTF}_{\text{synth}} \le 0.15$ | **BLOCKER** | High-resolution timer measuring audio chunk arrival | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-04** | Single-Model VRAM | GPU VRAM delta $\Delta\text{VRAM} = 0\text{ MiB}$ during voice execution | **BLOCKER** | `nvidia-smi` continuous polling before/during/after | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-05** | RAM Ceiling | Voice peak Working Set $< 450\text{ MB}$; System Free RAM $> 1.5\text{ GB}$ | **BLOCKER** | Host OS memory telemetry during simultaneous LLM + Voice | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-06** | Disk Retention | Zero audio bytes written to persistent filesystem | **BLOCKER** | Windows filesystem handle monitoring during session | **DESIGN ACCEPTED — VERIFICATION PENDING STAGE 2** |
| **GATE-VOICE-07** | Network Egress | Zero outbound socket connections and zero DNS queries | **BLOCKER** | ETW / socket monitoring throughout inference | **VERIFICATION PROTOCOL DEFINED — PENDING STAGE 2** |
| **GATE-VOICE-08** | Packaging Bounds | Total engine binaries + DLLs + models disk footprint $< 350\text{ MB}$ | **HIGH** | Directory size check in `%LOCALAPPDATA%\PICO\` | **DESIGN ACCEPTED** (~190–260 MB calculated) |
| **GATE-VOICE-09** | Fail-Closed Safety | Proper exception raised on missing binary or truncated model | **BLOCKER** | Unit test asserting fail-closed error without fallback mock | **DESIGN ACCEPTED — IMPLEMENTATION IN STAGE 3** |
| **GATE-VOICE-10** | License Review | Resolution of Piper GPL-3.0 redistribution compatibility or adoption of `sherpa-onnx` (Apache 2.0) | **BLOCKER** | Architecture & Legal Review Sign-Off | **PROVISIONAL — LEGAL REVIEW PENDING** |

---

## 8. Hardware Qualification Execution Plan (Next Steps)

1. **Stage 1 (Current)**: Architecture design, candidate evaluation, licensing review gate, and qualification protocol formalization (**DESIGN COMPLETE — HARDWARE QUALIFICATION PENDING**).
2. **Stage 2 (Benchmark Execution)**:
   - Procure verified binaries (`whisper-cli.exe` / `whisper.dll`, `piper.exe`) and canonical models into isolated staging workspace.
   - Execute physical qualification script (`tools/bench/voice_bench.py`).
   - Measure actual latency, RTF, WER, memory footprint, and network isolation telemetry on host hardware.
   - Publish empirical telemetry report in `docs/evaluation/voice-qualification-results.md`.
3. **Stage 3 (STT Implementation)**:
   - Implement `LocalWhisperSpeechToTextAdapter` adhering to `pico.perception.audio.adapter.BaseSpeechToTextAdapter`.
   - Connect live transcription to push-to-talk session completion and Policy A delivery.
4. **Stage 4 (TTS Implementation)**:
   - Implement `LocalTextToSpeechAdapter` adhering to `pico.perception.audio.adapter.BaseTextToSpeechAdapter` and returning valid WAV payloads conforming to `AudioSynthesisResponse`.
