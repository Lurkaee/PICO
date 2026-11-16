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
| **Processor** | Intel Core i5-13420H | 8 physical cores (4 Performance + 4 Efficient cores), 12 logical processors, AVX2 SIMD | Maximum 4 threads for STT inference; thread scheduling, core affinity, and UI/capture responsiveness to be empirically verified in Stage 2. CPU logical processor affinity has not yet been mapped to physical Performance cores (P-cores) or Efficient cores (E-cores); no automatic P-core scheduling compliance claim is made prior to empirical Stage 2 qualification evidence |
| **System Memory** | 16 GB DDR5 | ~15.64 GB visible, **~2.35 GB free physical RAM** | Peak Voice Working Set $< 450\text{ MB}$; free system RAM must remain $> 1.5\text{ GB}$ to prevent OS paging |
| **Discrete GPU** | NVIDIA GeForce RTX 3050 Laptop GPU | 4,094 MiB dedicated VRAM, Driver 592.82, CUDA 13.1 | **0 MiB VRAM for Voice** (Strategy A CPU execution); preserves 100% VRAM for Ollama LLM |
| **Disk Storage** | ~60 GB available (AGENTS.md target) | **5.48 GB free on host drive `C:`** *(time-sensitive)* | Total voice binaries + DLLs + model weights $< 350\text{ MB}$ design ceiling (estimated combined footprint ~115–267 MB; physical installed directory footprint to be measured in Stage 2); zero raw audio written to persistent disk |
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
- **Latency Hierarchy & TTFAC Definition**: Because PICO's existing TTS response contract is complete-buffer based, three distinct latency metrics are evaluated:
  1. **Engine-Internal Time-to-First-Audio-Chunk ($\text{TTFAC}_{\text{internal}}$)**: Wall-clock milliseconds from text input submission to the arrival of the first complete float32 audio chunk in memory via the `piper_audio_chunk` callback. Target: $\text{TTFAC}_{\text{internal}} < 200\text{ ms}$.
  2. **API Complete-WAV Response Latency ($T_{\text{response}}$)**: Total wall-clock milliseconds from text input submission until the adapter accumulates all chunks, converts float32 to PCM, constructs the RIFF/WAVE header, and returns the complete WAV buffer to `AudioSessionManager` (and base64-encodes it into `AudioSynthesisResponse`). Target: $T_{\text{response}} < 500\text{ ms}$ for short conversational turns (e.g. `tts_prompt_short`).
  3. **User-Visible First-Playable-Audio Latency ($T_{\text{first-playable}}$)**: Wall-clock milliseconds from user request until audio playback begins on the user's speaker ($T_{\text{first-playable}} \ge T_{\text{response}} + T_{\text{transport}} + T_{\text{decode}}$).
  - *Non-Negotiable Rule*: Engine-internal chunk arrival latency must NOT be labeled as user-visible TTFAC. Any future first-audio streaming playback would require an incremental transport (e.g. WebSocket audio frames or chunked SSE) outside the current contract.
- **Synthesis Duration ($T_{\text{synth}}$)**: Total wall-clock duration to synthesize the complete audio payload.
- **Synthesized Audio Duration ($T_{\text{audio\_out}}$)**: Duration of the synthesized speech output in seconds ($\text{num\_samples} / \text{sample\_rate}$).
- **Synthesis Real-Time Factor ($\text{RTF}_{\text{synth}}$)**:
  $$\text{RTF}_{\text{synth}} = \frac{T_{\text{synth, seconds}}}{T_{\text{audio\_out, seconds}}} = \frac{T_{\text{synth, milliseconds}}}{T_{\text{audio\_out, milliseconds}}}$$
  Target: $\text{RTF}_{\text{synth}} \le 0.15$ ($\ge 6.6\times$ faster than real-time).
- **Audio Conversion & Payload Conformance**:
  - The upstream `libpiper` C API emits 32-bit float audio chunks (`const float *samples`, normalized $[-1.0, 1.0]$) at the voice's configured sample rate (e.g. 22,050 Hz, 1 channel mono).
  - **Primary Format (Signed 16-bit PCM Little-Endian)**: The adapter clamps samples to $[-1.0, 1.0]$, scales by 32,767.0, converts to signed 16-bit little-endian integer (`int16_t`, 2 bytes per sample), and prepends a standard 44-byte RIFF/WAVE header (`wFormatTag = 1`, `nChannels = 1`, `nSamplesPerSec = 22050`, `wBitsPerSample = 16`, `nBlockAlign = 2`, `nAvgBytesPerSec = 44100`, `Subchunk2Size = num_samples * 2`, `ChunkSize = 36 + Subchunk2Size`).
  - **Alternative Format (IEEE-Float WAV)**: Retains raw float32 samples with `wFormatTag = 3` (`WAVE_FORMAT_IEEE_FLOAT`), `wBitsPerSample = 32`, `nBlockAlign = 4`, `Subchunk2Size = num_samples * 4`.
  - Full RIFF header validation: Verify `ChunkSize == len(wav_bytes) - 8` and `Subchunk2Size == len(pcm_bytes)`.
  - The resulting WAV byte buffer conforms strictly to `BaseTextToSpeechAdapter.synthesize() -> tuple[bytes, float]` and `AudioSynthesisResponse(audio_base64=..., audio_format="wav")` without contract modification.

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
   - Profile thread execution across logical processors using Windows Performance Counters / `psutil`.
   - CPU logical processor affinity has not yet been mapped to physical Performance cores (P-cores) or Efficient cores (E-cores); Windows thread scheduling across hybrid architectures is not guaranteed.
   - Empirical verification that STT worker threads (provisional limit of 4) do not starve the Windows audio capture loop (`sounddevice`) or the companion event loop.
   - No P-core scheduling compliance claim may be made before empirical Stage 2 qualification evidence is recorded.
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

- **Pinned Upstream Repository**: `https://huggingface.co/ggerganov/whisper.cpp` (Git revision `5359861c739e955e79d9a303bcbc70fb988958b1`).
- **Pinned Upstream Releases**: `v1.7.4` (resolved commit `8a9ad7844d6e2a10cddf4b92de4089d7ac2b14a9`) / `v1.9.5` (resolved commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`).
- **Build Target**: Windows x64 MSVC, AVX2 SIMD enabled, static runtime.

| Model Identifier | Parameter Count | Quantization | Canonical Source Repository | Canonical Filename | Published File Size | Canonical Upstream Git LFS SHA-256 Digest | Local Verification State |
|---|---|---|---|---|---|---|---|
| `whisper-base.en-q8_0` *(Default Primary)* | 74M | Q8_0 GGML | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-base.en-q8_0.bin` | 81,781,811 bytes (~81.8 MB) | `a4d4a0768075e13cfd7e19df3ae2dbc4a68d37d36a7dad45e8410c9a34f8c87e` | Upstream verified; local check pending Stage 2 |
| `whisper-base.en-f16` *(Unquantized Reference)* | 74M | F16 Standard | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-base.en.bin` | 147,964,211 bytes (~148 MB) | `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002` | Upstream verified; local check pending Stage 2 |
| `whisper-tiny.en-q5_1` *(Low-Spec Fallback)* | 39M | Q5_1 GGML | `https://huggingface.co/ggerganov/whisper.cpp` | `ggml-tiny.en-q5_1.bin` | 32,200,000 bytes (~32.2 MB) | `c77c5766f1cef09b6b7d47f21b546cbddd4157886b3b5d6d4f709e91e66c7c2b` | Upstream verified; local check pending Stage 2 |

#### 6.1.1 Whisper Native C-API Binding & ABI Stability Strategy
- The native C API `whisper_full(ctx, params, samples, n_samples)` receives `struct whisper_full_params` by value.
- Binding this 50+ field structure directly via Python `ctypes.Structure` is fragile across compiler toolchains and upstream revisions.
- Stage 2 will evaluate authoring a thin, project-owned C shim (`pico_whisper_shim.dll`) that compiles against pinned headers and exposes an opaque context with a minimal flat ABI.
- The Python ctypes loader must verify exported symbols and struct sizes, failing closed (`VoiceEngineABIMismatchError`) if an ABI divergence is detected.

### 6.2 Text-to-Speech Voices (`Piper ONNX`)

- **Pinned Upstream Voice Repository**: `https://huggingface.co/rhasspy/piper-voices` (Git revision `e9ab671f2a0e50a701b34361136ba3e6a3a9eb0c`).
- **Pinned Upstream Engine Releases**:
  - Legacy MIT Release: `rhasspy/piper` release `2023.11.14-2` (commit `38917ffd8c0e219c6581d73e07b30ef1d572fce1`), asset `piper_windows_amd64.zip` (22,477,236 bytes).
  - Maintained GPL-3.0 Release: `OHF-Voice/piper1-gpl` tag `v1.8.0` (commit `639388b6317fc4731e91d53da42aea68fd4166ff`).

#### 6.2.1 Voice Artifact Registry & Licensing Status

| Voice Identifier | Architecture / Sample Rate | Dataset & Training Lineage | Canonical Subpath | Published File Size | Canonical Upstream SHA-256 Digest | PICO Distribution Clearance |
|---|---|---|---|---|---|---|
| `en_US-lessac-medium.onnx` *(Provisional Research Benchmark)* | VITS ONNX (22,050 Hz) | Lessac Blizzard 2013 dataset (trained from scratch) | `en/en_US/lessac/medium/en_US-lessac-medium.onnx` | 63,201,294 bytes (~63.2 MB) | Git LFS SHA-256: `5efe09e69902187827af646e1a6e9d269dee769f9877d17b16b1b46eeaaf019f` | **NOT CLEARED FOR DEFAULT PICO DISTRIBUTION** (Blizzard 2013 non-commercial research restriction; provisional technical benchmark only) |
| `en_US-lessac-medium.onnx.json` *(Voice Config)* | JSON Config | Model inference and phoneme map | `en/en_US/lessac/medium/en_US-lessac-medium.onnx.json` | 4,885 bytes (~4.9 KB) | SHA-256: `efe19c417bed055f2d69908248c6ba650fa135bc868b0e6abb3da181dab690a0` | Associated config for research benchmark voice |
| `en_US-ljspeech-medium.onnx` *(Candidate Permissive Voice)* | VITS ONNX (22,050 Hz) | LJ Speech dataset (Public Domain; trained from scratch for 1000 epochs) | `en/en_US/ljspeech/medium/en_US-ljspeech-medium.onnx` | 63,531,379 bytes (~63.5 MB) | Git LFS SHA-256: `6f52a751e2349abe7a76735eb09dc1875298c77ea2342ffd2fef79ff81b87f22` | **EVALUATED PERMISSIVE CANDIDATE** (Clean Public Domain training lineage; candidate for default distribution) |
| `en_US-ljspeech-medium.onnx.json` *(Voice Config)* | JSON Config | Model inference and phoneme map | `en/en_US/ljspeech/medium/en_US-ljspeech-medium.onnx.json` | 4,972 bytes (~5.0 KB) | SHA-256: `141d612cc0a95ed7efc1ca936b845c2364967f2e9217c5dbfcf69fc4d6c65860` | Associated config for permissive candidate voice |

---

## 7. Pass/Fail Qualification Matrix & Promotion Gates

Before promoting Milestone P11.3 from **Stage 1 (Design)** to **Stage 3/4 (Runtime Implementation)**, the following gates must be physically verified:

| Gate ID | Area | Required Qualification Threshold | Severity | Verification Method | Current Stage 1 Status |
|---|---|---|---|---|---|
| **GATE-VOICE-01** | STT Latency | RTF $\le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Planned Stage 2 Benchmark Harness (`tools/bench/voice_bench.py`) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-02** | STT Accuracy | WER $\le 12.0\%$ on standard evaluation corpus | **BLOCKER** | Planned Stage 2 Benchmark Harness (Word Error Rate against ground truth) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-03** | TTS Latency | Complete-WAV $T_{\text{response}} < 500\text{ ms}$; $\text{TTFAC}_{\text{internal}} < 200\text{ ms}$; Synthesis $\text{RTF}_{\text{synth}} \le 0.15$ | **BLOCKER** | Planned Stage 2 Benchmark Harness (high-resolution timers measuring chunk arrival and complete WAV encoding) | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-04** | Single-Model VRAM | GPU VRAM delta $\Delta\text{VRAM} = 0\text{ MiB}$ during voice execution | **BLOCKER** | `nvidia-smi` continuous polling before/during/after | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-05** | RAM Ceiling | Voice peak Working Set $< 450\text{ MB}$; System Free RAM $> 1.5\text{ GB}$ | **BLOCKER** | Host OS memory telemetry during simultaneous LLM + Voice | **NOT MEASURED — HARDWARE QUALIFICATION PENDING** |
| **GATE-VOICE-06** | Disk Retention | Zero audio bytes written to persistent filesystem | **BLOCKER** | Windows filesystem handle monitoring during session | **DESIGN ACCEPTED — VERIFICATION PENDING STAGE 2** |
| **GATE-VOICE-07** | Network Egress | Zero outbound socket connections and zero DNS queries | **BLOCKER** | ETW / socket monitoring throughout inference | **VERIFICATION PROTOCOL DEFINED — PENDING STAGE 2** |
| **GATE-VOICE-08** | Packaging Bounds | Total engine binaries + DLLs + models disk footprint $< 350\text{ MB}$ design ceiling | **HIGH** | Directory size check in `%LOCALAPPDATA%\PICO\` | **DESIGN WITHIN BUDGET — PHYSICAL MEASUREMENT PENDING STAGE 2** (Estimated ~115–267 MB based on published sizes) |
| **GATE-VOICE-09** | Fail-Closed Safety | Proper exception raised on missing binary or truncated model | **BLOCKER** | Unit test asserting fail-closed error without fallback mock | **DESIGN ACCEPTED — IMPLEMENTATION IN STAGE 3** |
| **GATE-VOICE-10** | License Review | Resolution of Piper GPL-3.0 redistribution compatibility or adoption of `sherpa-onnx` (Apache 2.0) AND clearance of voice model dataset redistribution rights (Lessac research-only; LJSpeech public domain candidate) | **BLOCKER** | Architecture & Legal Review Sign-Off | **PROVISIONAL — LEGAL & REDISTRIBUTION REVIEW PENDING** |

---

## 8. Hardware Qualification Execution Plan & Stage 2 Tooling Status

### 8.1 Stage 2 Qualification Tooling Status
- **Planned Tool**: `tools/bench/voice_bench.py` **does not currently exist** in the repository. It is a planned Stage 2 deliverable.
- Stage 2 must include authoring, unit-testing, and executing this benchmark harness.
- **Required Harness Inputs**:
  - Deterministic 16 kHz 16-bit mono PCM test audio files (`audio_cmd_short_1s.wav`, `audio_cmd_query_3s.wav`, `audio_cmd_workflow_5s.wav`, `audio_cmd_dictation_10s.wav`).
  - Deterministic TTS text prompt corpus (`tts_prompt_short`, `tts_prompt_medium`, `tts_prompt_technical`, `tts_prompt_long`).
- **Telemetry Outputs**:
  - Processing wall-clock latencies (P50, P90, P95, max).
  - Calculated RTF and $\text{RTF}_{\text{synth}}$.
  - WER / CER against ground truth transcripts.
  - Physical memory (`WorkingSet64`, `PrivateMemorySize64`), `nvidia-smi` VRAM delta, and ETW network event logs.
  - Generation of structured qualification report: `docs/evaluation/voice-qualification-results.md`.
- **Pre-Execution Rule**: No benchmark results exist or may be claimed prior to Stage 2 harness development and physical execution on the target host.

### 8.2 Staged Progression
1. **Stage 1 (Current)**: Architecture design, candidate evaluation, licensing review gates, and qualification protocol formalization (**DESIGN COMPLETE — HARDWARE QUALIFICATION PENDING**).
2. **Stage 2 (Benchmark Tooling & Execution)**:
   - Develop and test `tools/bench/voice_bench.py`.
   - Procure verified binaries (`whisper-cli.exe` / `whisper.dll`, `piper.exe`) and canonical models into isolated staging workspace.
   - Execute physical qualification suite on host hardware.
   - Measure actual latency, RTF, WER, memory footprint, and network isolation telemetry.
   - Publish empirical telemetry report in `docs/evaluation/voice-qualification-results.md`.
3. **Stage 3 (STT Implementation)**:
   - Implement `LocalWhisperSpeechToTextAdapter` adhering to `pico.perception.audio.adapter.BaseSpeechToTextAdapter`.
   - Connect live transcription to push-to-talk session completion and Policy A delivery.
4. **Stage 4 (TTS Implementation)**:
   - Implement `LocalTextToSpeechAdapter` adhering to `pico.perception.audio.adapter.BaseTextToSpeechAdapter` and returning valid WAV payloads conforming to `AudioSynthesisResponse`.
