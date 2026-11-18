# PICO — Local STT (whisper.cpp) Hardware Qualification Report (Stage 2 Prototype)

- **Date**: 2026-11-18T10:39:20.039817+00:00
- **Commit**: `39700780f3c1d978a64f46ec14baa763b2d5c445`
- **Milestone**: P11.3.2 Stage 2 Native Whisper Qualification Prototype
- **Status**: Stage 2 Prototype — Incomplete / Partial Qualification
- **Host Hardware**: Intel64 Family 6 Model 186 Stepping 2, GenuineIntel
- **CPU Topology**: Intel Core i5-13420H (8 physical cores: 4P + 4E, 12 logical processors)
- **Execution Target**: CPU-only (4 threads, AVX2 enabled, GPU disabled)
- **Scheduler Note**: Inference configured with 4 worker threads; scheduler affinity was not explicitly bound to P-cores.
- **Responsiveness Note**: FastAPI event loop responsiveness and capture stream continuity under concurrent load were not measured during this prototype benchmark and are marked NOT RUN.
- **Runtime Engine**: whisper.cpp 1.9.5-dev
- **Model**: `ggml-base.en-q8_0.bin` (77.99 MB, SHA-256: `a4d4a0768075e13c...`)
- **Corpus Manifest**: `docs/evaluation/voice_corpus_manifest.json`

---

## 1. Executive Summary & Qualification Gates

| Gate ID | Area / Gate | Required Threshold | Measured Result | Qualification Status |
|---|---|---|---|---|
| `GATE-VOICE-01` | **STT Latency** | RTF <= 0.30 (5s audio in <= 1.5s) | 5s sample 'audio_cmd_workflow_5s.wav': End-to-End P50=1.1992s (RTF=0.2437), P90=1.2432s, P95=1.2432s, Max=1.2432s; Native-only P50=1.1817s (RTF=0.2402), P90=1.2268s, P95=1.2268s, Max=1.2268s [Breakdown: WAV parse=0.06ms, PCM norm=2.56ms, C buf=6.3ms, Native=1154.46ms] | **PASS** |
| `GATE-VOICE-02` | **STT Accuracy (WER)** | STT WER <= 12.0% on conversational English commands | Preliminary real-speech aggregate WER=2.67% (Macro WER=8.33%, CER=6.41%) on JFK speech slices; smoke commands (Windows SAPI) WER=0.0%. Qualification marked NOT RUN because the canonical command corpus has not been evaluated with human conversational speech and verified reference transcripts. | NOT RUN |
| `GATE-VOICE-03` | **TTS Latency** | TTFAC < 500ms; RTF_synth <= 0.15 | Deferred to Milestone P11.3.4 (Piper/sherpa-onnx qualification) | NOT RUN |
| `GATE-VOICE-04` | **Single-Model GPU VRAM** | VRAM Delta = 0 MiB (preserves Ollama LLM residency) | Historical observation: CPU isolation observed (0 MiB additional VRAM at sampled checkpoints; initial=2495 MiB, peak=2495 MiB, final=2495 MiB across 102 observations) and tag 'qwen3.5:2b' was reported GPU-resident (1772.9 MiB). Full co-residency qualification marked NOT RUN because model artifact digest continuity was not established by the historical benchmark run. | NOT RUN |
| `GATE-VOICE-05` | **System RAM Ceiling** | Peak Working Set < 450 MB and Available System RAM > 1.5 GB | Min Available System RAM 863.1 MB <= 1536.0 MB (1.5 GB threshold), Private Bytes: 810.29 MB across 2115 continuous samples | FAIL |
| `GATE-VOICE-06` | **Disk Retention** | 0 raw-audio bytes persisted to disk | 0 raw audio files created in process temp/working directories; independent kernel ETW minifilter filesystem audit unavailable without administrative elevation | NOT RUN |
| `GATE-VOICE-07` | **Network Egress** | 0 outbound socket connections & 0 DNS queries | 0 active socket connections detected for PID 32972; kernel ETW DNS-client activity tracing unavailable without elevation | NOT RUN |
| `GATE-VOICE-08` | **Packaging Bounds** | Combined voice subsystem footprint < 350 MB | STT Prototype: 80.92 MB (Binaries: 2.92 MB, Model: 77.99 MB). Combined STT+TTS voice subsystem gate deferred to Milestone P11.3.4. | NOT RUN |
| `GATE-VOICE-09` | **Fail-Closed Safety** | Deterministic typed exception on missing or corrupted artifact | Verified: missing model, truncated header, cryptographic SHA-256 digest mismatch, and missing runtime fail closed in isolated prototype fixtures (does not imply production adapter qualification). | **PASS** |
| `GATE-VOICE-10` | **License Review** | Permissive MIT license without copyleft risk (voice subsystem full qualification) | STT prototype components pass MIT review (whisper.cpp engine MIT, OpenAI Whisper weights MIT). Overall GATE-VOICE-10 marked NOT RUN pending resolution of Piper TTS licensing (GPL-3.0 in active fork vs. MIT / Apache-2.0 fallback). | NOT RUN |

---

## 2. Benchmark Results Across Test Corpus

| Audio Sample | Type | Duration | E2E P50 | Native P50 | E2E P90 | E2E P95 | E2E Max | E2E RTF | Native RTF | WER (%) | CER (%) | Emitted Transcript |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `audio_cmd_dictation_10s.wav` | synthetic_smoke | 11.025s | 1.224s | 1.1902s | 1.301s | 1.301s | 1.301s | **0.111** | 0.108 | 0.0% | 0.0% | "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically." |
| `audio_cmd_query_3s.wav` | synthetic_smoke | 3.405s | 1.068s | 1.0586s | 1.0985s | 1.0985s | 1.0985s | **0.3136** | 0.3109 | 0.0% | 0.0% | "What tasks are currently waiting for my approval?" |
| `audio_cmd_short_1s.wav` | synthetic_smoke | 1.775s | 1.0286s | 1.0228s | 1.0622s | 1.0622s | 1.0622s | **0.5795** | 0.5762 | 0.0% | 0.0% | "Pico Stop" |
| `audio_cmd_workflow_5s.wav` | synthetic_smoke | 4.92s | 1.1992s | 1.1817s | 1.2432s | 1.2432s | 1.2432s | **0.2437** | 0.2402 | 0.0% | 0.0% | "Summarize the latest system logs and create a new draft task for review." |
| `jfk.wav` | real_speech | 11.0s | 1.1922s | 1.1533s | 1.3096s | 1.3096s | 1.3096s | **0.1084** | 0.1048 | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_dictation_10s.wav` | real_speech | 10.0s | 1.223s | 1.1829s | 1.3008s | 1.3008s | 1.3008s | **0.1223** | 0.1183 | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_query_3s.wav` | real_speech | 3.2s | 1.1047s | 1.0958s | 1.3679s | 1.3679s | 1.3679s | **0.3452** | 0.3424 | 0.0% | 0.0% | "And so, my fellow Americans!" |
| `jfk_short_1s.wav` | real_speech | 1.5s | 1.1287s | 1.1232s | 1.4561s | 1.4561s | 1.4561s | **0.7525** | 0.7488 | 50.0% | 38.5% | "And so I felt" |
| `jfk_workflow_5s.wav` | real_speech | 5.1s | 1.0766s | 1.0569s | 1.1738s | 1.1738s | 1.1738s | **0.2111** | 0.2072 | 0.0% | 0.0% | "And so my fellow Americans, ask not!" |
| `vosk_digits.wav` | real_speech | 8.308s | 1.1452s | 1.1158s | 1.2863s | 1.2863s | 1.2863s | **0.1378** | 0.1343 | 0.0% | 0.0% | "1 0 0 0 1 90210 01803" |

---

## 3. Accuracy Metrics Breakdown

- **Real Speech Aggregate WER**: 2.67% (total edits / total reference words)
- **Real Speech Macro-Average WER**: 8.33% (unweighted mean across clips)
- **Real Speech Macro-Average CER**: 6.41%
- **Synthetic Commands Smoke WER**: 0.0% (reported separately from real speech)

---

## 4. Hardware & Resource Telemetry

- **Cold Start Initialization Time**: 0.2481s (PicoWhisperEngine.__init__() - full constructor: artifact SHA-256 verification, DLL load, native context prep)
- **Peak Working Set Memory**: 331.95 MB
- **Minimum Available System RAM**: 863.1 MB (threshold > 1.5 GB, continuously sampled across 2115 intervals)
- **Initial GPU VRAM**: 2495 MiB
- **Peak GPU VRAM Observed**: 2495 MiB (102 observations, sampled per iteration (~every 1s))
- **Final GPU VRAM**: 2495 MiB
- **Maximum GPU VRAM Delta**: 0 MiB
- **Ollama LLM Co-Residency**: Model 'qwen3.5:2b' (Canonical PICO local-small LLM model tag) resident in GPU VRAM (1772.9 MiB)
- **Average Real-Time Factor (E2E RTF)**: **0.2925**

---

## 5. Architectural Invariants Verification

1. **Zero Raw-Audio Disk Retention**: Evaluated in-memory pointer pipeline. 0 audio files found in process temporary directories. Full OS kernel minifilter audit marked NOT RUN.
2. **Zero Cloud Egress**: Monitored socket connections for PID. Full kernel ETW DNS tracing marked NOT RUN.
3. **In-Memory Buffer Zeroization**: Best-effort zeroization scrubs mutable application arrays (ctypes float arrays, lists, bytearrays). Immutable Python objects and native C++ heap buffers are freed upon context teardown.
4. **Fail-Closed Protection**: Verified immediate typed exceptions upon missing model, truncated model header, cryptographic SHA-256 digest mismatch, and missing runtime in isolated fixtures.
5. **Production Boundary**: Production adapter (`LocalWhisperSpeechToTextAdapter`) remains strictly disabled (`is_available() == False`) and fails closed.

---

## 6. Stage 3 Readiness Assessment & Preflight Audit

- **Audit Date**: 2026-11-18T22:30:00+05:30
- **Audited Main Commit**: `6f39fd74dce5c30850792d55993ab30b1873c8fd`
- **Evaluation Target**: Milestone P11.3 Stage 3 (Local STT Runtime Integration) Readiness

### 6.1 Resource Preflight Telemetry

A non-invasive, read-only preflight inspection of host memory and compute resources was conducted:

| Metric | Measured Value | Qualification Threshold | Compliance Assessment |
|---|---|---|---|
| **Total System RAM** | 15.64 GB (16,791,048,192 bytes) | 16 GB Physical | Compliant |
| **Available System RAM** | 2.41 GB (2,472.8 MB, 2,592,919,552 bytes) | > 1,536.0 MB (1.5 GB) | Exceeds idle threshold; however, physical test under load historically dipped to 863.1 MB |
| **Dedicated GPU VRAM** | Total: 4,094.0 MiB, Used: 0.0 MiB, Free: 3,892.0 MiB | 0 MiB Voice Consumption | VRAM available; LLM residency unverified |
| **Ollama Model Residency** | 0 models resident (`/api/ps` returned 0 active instances) | `qwen3.5:2b` resident in VRAM | **NOT MET** (`qwen3.5:2b` installed but not resident) |

**Top Host Memory Consuming Processes**:
1. `chrome.exe` (PID 20660): 804.09 MB
2. `MsMpEng.exe` (PID 4268): 768.48 MB
3. `agy.exe` (PID 18160): 706.17 MB
4. `Antigravity IDE.exe` (PIDs 3312, 5756, 23336, 3240, 13084): ~2.25 GB aggregate
5. `vmmemWSL` (PID 6072): 327.51 MB

### 6.2 Pre-Requisite Audit for Stage 3

1. **Canonical Command Corpus & STT Accuracy (`GATE-VOICE-02`)**:
   - Canonical Requirement (ADR 0005 §2.5 & §6; Qualification Protocol §3.1.2 & §7): STT Word Error Rate (WER) $\le 12.0\%$ on conversational English commands across the standard command audio corpus:
     - `audio_cmd_short_1s.wav` ("PICO stop.")
     - `audio_cmd_query_3s.wav` ("What tasks are currently waiting for my approval?")
     - `audio_cmd_workflow_5s.wav` ("Summarize the latest system logs and create a new draft task for review.")
     - `audio_cmd_dictation_10s.wav` ("We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically.")
   - Manifest Status (`docs/evaluation/voice_corpus_manifest.json`): The current audio fixtures comprise synthetic Windows SAPI TTS clips for the command corpus, alongside 5 slices of a single historical speech (JFK 1961 inaugural address) and 1 single-speaker numeric digit sequence (`vosk_digits.wav`).
   - The manifest explicitly records: *"The current corpus does NOT constitute a representative multi-speaker conversational evaluation. Formal accuracy qualification (GATE-VOICE-02) remains NOT RUN until a diverse multi-speaker conversational dataset is evaluated in a future milestone."*
   - Specification Drift Correction: Demographic minimums (such as 5–10 speakers across specific genders, age groups, or accents) are not stated in ADR 0005 or the qualification protocol and do not constitute formal qualification criteria. A broader multi-speaker dataset may provide supplementary evidence, but it does not replace the canonical requirement for accuracy evaluation against the standard conversational command corpus.
   - Status: **NOT RUN** (Accuracy qualification on the canonical command corpus with genuine human conversational speech and verified reference transcripts remains incomplete).

2. **Single-Model GPU VRAM & Co-Residency (`GATE-VOICE-04`)**:
   - Ollama `api/ps` reported 0 active models during preflight.
   - PICO operating rules strictly forbid automated warmup, pulling, or loading of models.
   - In accordance with benchmark Gate 04 evaluation rules, missing model residency results in **NOT RUN**.

3. **RAM Ceiling Under Concurrent Load (`GATE-VOICE-05`)**:
   - While idle available RAM is currently 2,472.8 MB (> 1,536.0 MB), historical benchmark load testing recorded an available RAM minimum of 863.1 MB under concurrent development load.
   - Status: **FAIL** (Historical qualification failure preserved until full benchmark can be cleanly executed with resident LLM).

4. **Privacy & Elevation Audits (`GATE-VOICE-06` & `GATE-VOICE-07`)**:
   - Current security context verified: `Administrator == False` (non-elevated session).
   - Independent kernel ETW DNS-client activity tracing and filesystem minifilter audits require administrative elevation.
   - In accordance with non-negotiable security boundaries, Windows security controls are preserved without modification.
   - Status: **NOT RUN** (Process-level zero sockets and zero temp disk files confirmed).

### 6.3 Unresolved Stage 3 Promotion-Rule Ambiguity

A genuine structural ambiguity exists across canonical project documents:
- **Implementation Plan Sequencing**: `docs/implementation-plan.md` sequences Milestone P11.3 into distinct vertical stages: Stage 3 implements Local Speech-to-Text (STT) runtime integration, while Stage 4 independently implements Local Text-to-Speech (TTS) runtime integration.
- **Qualification Protocol Promotion Gates**: `docs/evaluation/voice-engine-qualification.md` §7 specifies a single unified table of ten blocker gates (including `GATE-VOICE-03` TTS Latency and `GATE-VOICE-10` Piper GPL-3.0 vs. Apache-2.0 License Review) that must be physically verified *"Before promoting Milestone P11.3 from Stage 1 (Design) to Stage 3/4 (Runtime Implementation)"*.
- **Ambiguity Assessment**:
  - The protocol treats all 10 gates as prerequisites before proceeding to either Stage 3 or Stage 4, whereas the implementation plan decouples STT (Stage 3) from TTS (Stage 4).
  - This architectural ambiguity has not been formally resolved through an ADR amendment or qualification protocol revision.
  - However, resolving this ambiguity is **not a blocker for this assessment**: even if TTS-specific gates (`GATE-VOICE-03`, `GATE-VOICE-10`) were scoped exclusively to Stage 4, Stage 3 STT integration remains **NOT AUTHORIZED** because the core STT-specific gates (`GATE-VOICE-02` Accuracy, `GATE-VOICE-04` GPU Co-Residency, `GATE-VOICE-05` RAM Ceiling, and `GATE-VOICE-06`/`07` Privacy) remain failed or incomplete.

### 6.4 Stage 3 Authorization Decision

- **DECISION: P11.3 STAGE 3 INTEGRATION IS NOT AUTHORIZED**
- **Rationale**:
  - `GATE-VOICE-02` (STT Accuracy): Canonical command corpus has not been verified with genuine human conversational speech and verified reference transcripts.
  - `GATE-VOICE-04` (Single-Model GPU VRAM): Ollama LLM was not resident during preflight; same-artifact digest continuity under voice execution was not established.
  - `GATE-VOICE-05` (RAM Ceiling): Available system RAM dipped to 863.1 MB ($\le 1,536.0\text{ MB}$) during historical physical benchmark load testing, failing the memory headroom ceiling.
  - `GATE-VOICE-06` & `GATE-VOICE-07` (Privacy): Elevated Windows kernel ETW DNS-client and filesystem minifilter trace audits were unavailable.
  - Promotion-Rule Ambiguity: Unresolved tension between unified protocol gates and staged STT/TTS roadmap.
- **Production Guardrails Maintained**:
  - `LocalWhisperSpeechToTextAdapter` in `pico/perception/audio/adapter.py` remains strictly disabled (`is_available() == False`).
  - Production audio delivery endpoints continue to fail closed with `ValidationError("Speech recognition is disabled")`.
  - Zero model weights or binary DLLs are committed to version control.
