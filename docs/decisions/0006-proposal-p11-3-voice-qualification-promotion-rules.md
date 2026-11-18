# 0006. Architecture Decision Proposal: P11.3 Voice Qualification Promotion Rules & Corpus Interpretation

- **Date**: 2026-11-18
- **Status**: PROPOSED (Architecture Decision Proposal — Pending Project Owner Review & Approval)
- **Author**: PICO Architecture & Engineering Team
- **Deciders**: Project Owner / Lead Architect
- **Consulted**: `AGENTS.md`, `docs/architecture.md`, `docs/implementation-plan.md`, `docs/decisions/0005-local-voice-engine-selection-and-hardware-qualification.md`, `docs/evaluation/voice-engine-qualification.md`, `docs/evaluation/voice-qualification-results.md`, `docs/evaluation/voice_corpus_manifest.json`

---

## 1. Executive Summary & Proposal Boundary

> [!IMPORTANT]
> **PROPOSAL STATUS — NON-NORMATIVE NOTICE:**
> This document is an **architectural proposal submitted for project owner review and explicit decision**. It does **NOT** amend, supersede, or modify the accepted baseline of [ADR 0005](0005-local-voice-engine-selection-and-hardware-qualification.md), [Qualification Protocol](../evaluation/voice-engine-qualification.md), or [Implementation Plan](../implementation-plan.md).
> 
> Furthermore, this proposal does **NOT** authorize or activate Milestone P11.3 Stage 3 (Local Speech-to-Text Runtime Integration) or Stage 4 (Local Text-to-Speech Runtime Integration). Production speech recognition remains strictly disabled (`LocalWhisperSpeechToTextAdapter.is_available() == False`) and fails closed with `ValidationError("Speech transcription runtime is unavailable")`.

This proposal addresses two structural ambiguities identified during the closure of Milestone P11.3.2 Stage 2 and recorded in PR #94:
1. **The Stage 3 / Stage 4 Qualification Scope Ambiguity**: The tension between the sequential implementation plan (which separates STT Stage 3 from TTS Stage 4) and the qualification protocol §7 (which lists a unified set of ten gates as blockers before Stage 3/4).
2. **The `GATE-VOICE-02` Accuracy Corpus Acceptance Interpretation**: The tension between the ADR requirement for "conversational English commands", the protocol specification of four named command clips, and the reality that those clips in the current manifest are synthetic Windows SAPI recordings.

This proposal provides a concrete, stage-specific gate decomposition and evaluates two corpus interpretation options for project owner sign-off.

---

## 2. Conflicting Canonical Statements (Problem Statement)

### 2.1 Conflict 1: Staged Implementation Roadmap vs. Unified Blocker Matrix

Across canonical PICO architecture documents, two mutually incompatible promotion models currently exist:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ DOCUMENTATION CONFLICT: PROMOTION TIMING                                        │
├─────────────────────────────────────────────────────────────────────────────────┤
│ docs/implementation-plan.md:                                                    │
│   Sequences P11.3 into distinct, decoupled vertical stages:                     │
│     - Stage 3: Local Speech-to-Text (STT) Runtime Integration                   │
│     - Stage 4: Local Text-to-Speech (TTS) Runtime Integration                   │
│                                                                                 │
│ docs/evaluation/voice-engine-qualification.md §7:                                │
│   "Before promoting Milestone P11.3 from Stage 1 (Design) to                    │
│    Stage 3/4 (Runtime Implementation), the following gates must be              │
│    physically verified: [Lists all 10 gates]"                                   │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- **Implementation Plan Model (`docs/implementation-plan.md` items 405 & 406)**:
  - Sequences Milestone P11.3 into incremental, reviewable vertical slices (aligned with `AGENTS.md` Principle 9: *Incremental, Reviewable Slices*).
  - P11.3 Stage 3 implements `LocalWhisperSpeechToTextAdapter` and verifies push-to-talk speech perception independently.
  - P11.3 Stage 4 subsequently implements `LocalTextToSpeechAdapter` and verifies speech synthesis.
- **Qualification Protocol Model (`docs/evaluation/voice-engine-qualification.md` §7)**:
  - Presents a single monolithic table of ten promotion gates under the preamble: *"Before promoting Milestone P11.3 from Stage 1 (Design) to Stage 3/4 (Runtime Implementation), the following gates must be physically verified"*.
  - Marks both `GATE-VOICE-03` (TTS Latency: TTFAC $< 500\text{ ms}$, $\text{RTF}_{\text{synth}} \le 0.15$) and `GATE-VOICE-10` (Piper GPL-3.0 vs. Apache-2.0 License Review) as **BLOCKER**s.
- **Operational Tension**:
  - Taken literally, Protocol §7 blocks the implementation of the STT adapter (Stage 3) on Piper TTS latency measurements and Piper legal licensing decisions, even though the STT engine (`whisper.cpp` / MIT license) is completely decoupled from the TTS engine.
  - Conversely, proceeding with Stage 3 without formal stage-specific gating risks bypassing shared system resource checks (RAM, VRAM, and privacy egress).

---

### 2.2 Conflict 2: `GATE-VOICE-02` Accuracy Corpus Grounding

A three-way divergence exists between the architectural requirement, the protocol corpus specification, and the actual test audio manifest:

1. **ADR 0005 Requirement (§2.5 & §6)**:
   - Sets the canonical acceptance target:
     $$\text{STT Word Error Rate (WER)} \le 12.0\%\text{ on conversational English commands}$$
2. **Qualification Protocol §3.1.2 Specification**:
   - Formalizes the benchmark corpus as four specific audio clips and transcripts:
     - `audio_cmd_short_1s.wav`: `"PICO stop."`
     - `audio_cmd_query_3s.wav`: `"What tasks are currently waiting for my approval?"`
     - `audio_cmd_workflow_5s.wav`: `"Summarize the latest system logs and create a new draft task for review."`
     - `audio_cmd_dictation_10s.wav`: `"We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically."`
3. **Corpus Manifest Reality (`docs/evaluation/voice_corpus_manifest.json`)**:
   - The four command files in the repository are generated via **Windows SAPI TTS** (`David Desktop, synthetic en-US`).
   - The manifest includes an explicit qualification scope limitation:
     > *"The current corpus does NOT constitute a representative multi-speaker conversational evaluation. Formal accuracy qualification (GATE-VOICE-02) remains NOT RUN until a diverse multi-speaker conversational dataset is evaluated in a future milestone."*
4. **Specification Drift History**:
   - During earlier qualification passes, an unstated requirement for a minimum of 5–10 human speakers across diverse genders, ages, and regional accents was inadvertently introduced.
   - PR #94 removed this specification drift because neither ADR 0005 nor the Qualification Protocol specifies a demographic quota.
   - However, the core question remains: **Can synthetic Windows SAPI audio satisfy formal qualification of "conversational English commands", or does formal qualification require naturally spoken human recordings of the canonical command texts?**

---

## 3. Proposed Stage-Specific Gate Matrix

To reconcile the sequential delivery model with rigorous hardware and safety gating, this proposal decomposes the unified ten-gate matrix into two decoupled, verifiable milestone stages.

Every canonical numerical threshold and severity classification from Protocol §7 is strictly preserved.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ PROPOSED STAGE-SPECIFIC PROMOTION DECOMPOSITION                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   STAGE 3: LOCAL STT RUNTIME PROMOTION GATES                                    │
│   ├── GATE-VOICE-01 : STT Latency (RTF <= 0.30, 5s audio <= 1.5s)    [BLOCKER]  │
│   ├── GATE-VOICE-02 : STT Accuracy (WER <= 12.0% on command corpus)   [BLOCKER]  │
│   ├── GATE-VOICE-04 : Single-Model VRAM (Delta VRAM = 0 MiB, Ollama)  [BLOCKER]  │
│   ├── GATE-VOICE-05 : RAM Ceiling (Voice peak < 450 MB, Free > 1.5 GB)[BLOCKER] │
│   ├── GATE-VOICE-06 : Zero Audio Disk Retention & Buffer Zeroization  [BLOCKER]  │
│   ├── GATE-VOICE-07 : Zero Network & DNS Egress Isolation             [BLOCKER]  │
│   ├── GATE-VOICE-08 : STT Intermediate Packaging Budget (<= 150 MB)   [HIGH]     │
│   ├── GATE-VOICE-09 : STT Fail-Closed Safety (Missing/Corrupt Model)  [BLOCKER]  │
│   └── STT Licensing : Verified Permissive MIT License for whisper.cpp [BLOCKER]  │
│                                                                                 │
│   STAGE 4: LOCAL TTS RUNTIME PROMOTION GATES                                    │
│   ├── GATE-VOICE-03 : TTS Latency (TTFAC < 500 ms, RTF_synth <= 0.15) [BLOCKER] │
│   ├── GATE-VOICE-08 : Combined Voice Packaging Budget (< 350 MB Total)[HIGH]     │
│   ├── GATE-VOICE-10 : Complete Voice Subsystem License Review         [BLOCKER]  │
│   │                   (Piper GPL-3.0 Compatibility or Sherpa-ONNX)              │
│   └── Combined Voice Resource & Privacy Re-Verification:                        │
│       ├── Re-verify GATE-VOICE-04 (Co-residency with STT + TTS + LLM) [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-05 (Combined Working Set < 500 MB)     [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-06 (TTS Audio Memory Zeroization)      [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-07 (TTS Zero Network/DNS Egress)       [BLOCKER]  │
│       └── Re-verify GATE-VOICE-09 (TTS Fail-Closed Safety)            [BLOCKER]  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

### 3.1 Stage 3 Promotion Matrix (Local Speech-to-Text Runtime Integration)

Before promoting PICO to **Milestone P11.3 Stage 3** (activating `LocalWhisperSpeechToTextAdapter` in production), the following gates must be satisfied:

| Gate ID | Target Requirement | Severity | Verification Method | Stage 3 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-01`** | STT $\text{RTF} \le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Deterministic benchmark on `audio_cmd_workflow_5s.wav` | **Mandatory Precondition**. Passed historically ($\text{RTF}=0.2437$, P50=1.1992s). |
| **`GATE-VOICE-02`** | STT $\text{WER} \le 12.0\%$ on conversational English commands | **BLOCKER** | Word Error Rate evaluation across canonical command clips | **Mandatory Precondition**. Currently `NOT RUN`. Requires owner selection between Option A and Option B below. |
| **`GATE-VOICE-04`** | GPU $\Delta\text{VRAM} = 0\text{ MiB}$; single-model Ollama LLM residency preserved with zero eviction | **BLOCKER** | `nvidia-smi` telemetry with resident `qwen3.5:2b` | **Mandatory Precondition**. Currently `NOT RUN`. Requires pre-warmed Ollama LLM during test. |
| **`GATE-VOICE-05`** | STT peak Working Set $< 450\text{ MB}$; System Available RAM $> 1,536.0\text{ MB}$ under load | **BLOCKER** | Continuous memory sampler during simultaneous LLM + STT load | **Mandatory Precondition**. Currently `FAIL` (863.1 MB recorded under historical test load). |
| **`GATE-VOICE-06`** | Zero audio bytes written to persistent storage; mutable capture buffers zeroized | **BLOCKER** | Process file handle monitoring & buffer inspection | **Mandatory Precondition**. Currently `NOT RUN` (verified in prototype; elevated kernel audit required). |
| **`GATE-VOICE-07`** | Zero outbound socket connections and zero DNS queries during transcription | **BLOCKER** | Socket inspection & DNS event monitoring during inference | **Mandatory Precondition**. Currently `NOT RUN` (verified 0 sockets; elevated ETW trace required). |
| **`GATE-VOICE-08` (STT Slice)** | STT engine binary + DLLs + model footprint $\le 150\text{ MB}$ on disk | **HIGH** | Directory size check of STT bundle components | **Intermediate Milestone Check**. Prototype footprint is 80.92 MB (well within the STT slice). Treated as HIGH (see §3.3). |
| **`GATE-VOICE-09`** | Deterministic typed exceptions on missing binary or corrupted model in integrated adapter | **BLOCKER** | Unit & integration tests asserting fail-closed behavior without fallback mocks | **Mandatory Precondition**. Validated on isolated prototype; must be re-verified on production adapter. |
| **STT Component Licensing** | Permissive open-source license (MIT) for Whisper runtime, shim, and GGML model | **BLOCKER** | Legal audit of `whisper.cpp` (MIT) and GGML weights | **Mandatory Precondition**. Prototype STT tooling is 100% MIT. Compliant. |

---

### 3.2 Stage 4 Promotion Matrix (Local Text-to-Speech Runtime Integration)

Before promoting PICO to **Milestone P11.3 Stage 4** (activating `LocalTextToSpeechAdapter` in production), the remaining voice gates must be satisfied:

| Gate ID | Target Requirement | Severity | Verification Method | Stage 4 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-03`** | TTS Time-to-First-Audio-Chunk (TTFAC) $< 500\text{ ms}$; Synthesis $\text{RTF}_{\text{synth}} \le 0.15$ | **BLOCKER** | High-resolution timer measuring synthesis on benchmark prompt corpus | **Mandatory Precondition for TTS**. Evaluates Piper ONNX or sherpa-onnx. |
| **`GATE-VOICE-08` (Combined Subsystem)** | Combined STT + TTS binaries, DLLs, and models disk footprint $< 350\text{ MB}$ total | **HIGH** | Installer staging check (`tools/packaging/stage_windows_bundle.py`) | **Release Packaging Gate**. Verifies total footprint before Windows NSIS distribution (see §3.3). |
| **`GATE-VOICE-10`** | Complete voice subsystem licensing: resolution of Piper GPL-3.0 redistribution compatibility or adoption of `sherpa-onnx` (Apache 2.0) | **BLOCKER** | Formal legal & packaging architecture review sign-off | **Mandatory Precondition for TTS**. Prohibits shipping unreviewed copyleft binaries in NSIS bundle. |
| **Combined Resource & Privacy Re-Verification** | Coexistence of STT + TTS + LLM: $\Delta\text{VRAM}=0\text{ MiB}$, Combined Working Set $< 500\text{ MB}$, Available RAM $> 1.5\text{ GB}$, zero disk retention, zero egress | **BLOCKER** | Comprehensive multi-modal benchmark executing simultaneous STT and TTS | **Mandatory Precondition for Voice System Completion**. Prevents resource regression when both voice runtimes are active. |

---

### 3.3 Treatment and Severity of `GATE-VOICE-08` (Packaging Bounds)

A key operational nuance concerns **`GATE-VOICE-08`**, which is classified as **`HIGH`** in Qualification Protocol §7, rather than **`BLOCKER`**:

1. **Protocol Definition**:
   - Threshold: Total voice engine binaries, shared runtime DLLs, and voice models must remain $< 350\text{ MB}$ combined.
   - Severity: `HIGH` (important release quality attribute, but not a catastrophic runtime stability blocker like memory exhaustion or privacy leakage).
2. **Treatment at Stage 3 (STT Integration)**:
   - At Stage 3, only the STT runtime (`whisper.dll` / `pico_whisper_shim.dll` ~3.2 MB) and STT model (`ggml-base.en-q8_0.bin` ~81.8 MB) are installed, totaling **80.92 MB**.
   - The full $350\text{ MB}$ threshold cannot be fairly assessed at Stage 3 because the TTS runtime and voice models are not yet deployed.
   - **Proposed Rule**: At Stage 3, `GATE-VOICE-08` is treated as an **intermediate verification check** (STT component footprint $\le 150\text{ MB}$, severity `HIGH`). It is non-blocking for STT runtime integration as long as the STT footprint remains within its allocated slice.
3. **Treatment at Stage 4 (TTS & Full System Packaging)**:
   - At Stage 4, the combined voice subsystem (STT + Piper/Sherpa TTS + ONNX Runtime + voice models) is staged into the Windows package.
   - **Proposed Rule**: At Stage 4, `GATE-VOICE-08` evaluates the full combined footprint ($< 350\text{ MB}$). While classified `HIGH` during intermediate development, **formal packaging and distribution in the Windows NSIS installer requires 100% compliance** to respect host disk constraints (~5.48 GB available disk space on target hardware).

---

## 4. Resolution of `GATE-VOICE-02` Corpus Interpretation

### 4.1 Options for Owner Review

The project owner is presented with two explicit, well-defined options for resolving `GATE-VOICE-02`:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ GATE-VOICE-02 CORPUS INTERPRETATION OPTIONS                                     │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   OPTION A: DETERMINISTIC SAPI BENCHMARK (Synthetic Acceptance)                 │
│   ├── Formal Gate: Evaluate WER on the 4 existing Windows SAPI WAV files.       │
│   ├── Target: WER <= 12.0% across the 4 deterministic SAPI clips.               │
│   ├── Human Testing: Treated as non-blocking supplementary telemetry.           │
│   ├── Strength: 100% reproducible, zero acoustic variance, already staged.      │
│   └── Weakness: Synthetic SAPI phonemes do not represent real human acoustics.   │
│                                                                                 │
│   OPTION B: CANONICAL HUMAN RECORDING BENCHMARK (Recommended)                   │
│   ├── Formal Gate: Record the exact text of the 4 canonical commands spoken     │
│   │   by a natural human voice through a real microphone.                       │
│   ├── Target: WER <= 12.0% on the natural human command recordings.             │
│   ├── Demographic Quotas: Explicitly rejected (no 5-10 speaker requirement).    │
│   ├── SAPI Testing: Retained as automated CI regression fixtures.               │
│   ├── Strength: Truthful validation of spoken operator interaction.             │
│   └── Weakness: Requires recording and staging verified human audio files.      │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

#### Option A: Accept Deterministic SAPI-Generated Command Samples for Formal Protocol
- **Specification**:
  - Accept the four existing synthetic Windows SAPI command clips (`audio_cmd_short_1s.wav`, `audio_cmd_query_3s.wav`, `audio_cmd_workflow_5s.wav`, `audio_cmd_dictation_10s.wav`) as the authoritative corpus for formal `GATE-VOICE-02` qualification.
  - If measured $\text{WER} \le 12.0\%$ on these clips, `GATE-VOICE-02` is marked **PASS**.
  - Naturally spoken human audio recordings (e.g. JFK inaugural speech slices, supplemental operator recordings) are treated strictly as non-blocking, informational telemetry.
- **Trade-Off Analysis**:
  - *Advantages*:
    - Completely deterministic and reproducible across all developer and CI machines.
    - Zero acoustic variance, zero recording environment noise, zero speaker pronunciation variability.
    - Test fixtures are already present in the repository with verified Git LFS SHA-256 digests.
  - *Disadvantages*:
    - Windows SAPI TTS voices ("David Desktop") have rigid cadence, perfect phoneme timing, and synthetic spectral envelopes that do not reflect human vocal tract resonance, breathing, micro-pauses, or microphone proximity effects.
    - Passing an accuracy gate on synthetic SAPI clips provides zero empirical assurance that Whisper will accurately transcribe real human speech during daily operator push-to-talk usage.

#### Option B: Require Naturally Spoken Human Recordings of the Canonical Commands (RECOMMENDED)
- **Specification**:
  - Formal qualification of `GATE-VOICE-02` requires recording the **exact text of the four canonical commands** spoken by a genuine human operator through a physical microphone under standard room acoustic conditions.
  - In accordance with the removal of specification drift, **no arbitrary demographic or multi-speaker minimum (e.g. 5–10 speakers across specific genders/accents) is required**. A clean, verified recording of the four canonical command texts by a representative human speaker satisfies the formal gate.
  - The four existing SAPI-generated clips are retained in the test suite as deterministic smoke-test fixtures for automated CI runs, but cannot satisfy formal `GATE-VOICE-02` qualification.
- **Trade-Off Analysis**:
  - *Advantages*:
    - Truthful, empirical validation of real human speech recognition, directly fulfilling ADR 0005 §2.5 (*"conversational English commands"*).
    - Protects the system against false confidence: ensures the operator will experience reliable command recognition before Milestone P11.3 Stage 3 is promoted to production.
    - Adheres strictly to the four canonical command texts defined in Qualification Protocol §3.1.2 without inventing non-canonical demographic requirements.
  - *Disadvantages*:
    - Requires capturing, verifying, and committing four new 16 kHz 16-bit mono WAV files and their corresponding manifest metadata into `docs/evaluation/voice_corpus_manifest.json`.

---

### 4.2 Recommendation & Explicit Rationale

**The PICO Architecture & Engineering Team strongly recommends OPTION B.**

#### Technical & Architectural Rationale:
1. **Adherence to Core Principles (`AGENTS.md` Principles 1 & 10)**:
   - PICO is an autonomous personal intelligence coordinator, not a chatbot wrapper. Its speech perception capability directly determines whether spoken operator commands are faithfully transcribed into Policy A action proposals.
   - Principle 10 (*Truthful Reporting*) mandates that test outcomes reflect reality: claiming an STT engine is qualified for production based solely on transcribing synthetic Windows text-to-speech audio is fundamentally misleading.
2. **Alignment with ADR 0005 Intent**:
   - ADR 0005 §2.5 explicitly specifies *"conversational English commands"*. A synthetic speech synthesizer reading text is not a "conversational command"; it is an acoustic loopback artifact.
3. **Simplicity and Feasibility**:
   - Option B does not require complex academic speech corpora or large-scale demographic sampling. It simply requires recording the four canonical command texts (`"PICO stop."`, `"What tasks are currently waiting for my approval?"`, `"Summarize the latest system logs..."`, `"We need to verify the database migrations..."`) once with a real microphone. This is a finite, verifiable task that provides genuine engineering confidence.

---

## 5. Current Qualification Status & Why Stage 3 Remains Blocked

Regardless of which option the project owner selects, **Milestone P11.3 Stage 3 is currently NOT AUTHORIZED and remains strictly BLOCKED**.

The existing empirical qualification report ([docs/evaluation/voice-qualification-results.md](../evaluation/voice-qualification-results.md)) records the following verified baseline:

| Gate ID | Category | Status | Blocking Reason for Stage 3 |
| :--- | :--- | :--- | :--- |
| **`GATE-VOICE-01`** | STT Latency | **PASS** | Historical benchmark passed (P50=1.1992s, $\text{RTF}=0.2437 \le 0.30$). Non-blocking. |
| **`GATE-VOICE-02`** | STT Accuracy | **NOT RUN** | **BLOCKER**: Canonical command corpus accuracy is unmeasured with real human speech. |
| **`GATE-VOICE-03`** | TTS Latency | **NOT RUN** | Deferred to Stage 4 under the proposed stage-specific gate decomposition. |
| **`GATE-VOICE-04`** | Single-Model VRAM | **NOT RUN** | **BLOCKER**: Preflight showed 0 Ollama models resident; co-residency digest continuity unverified. |
| **`GATE-VOICE-05`** | RAM Ceiling | **FAIL** | **BLOCKER**: Available RAM dropped to 863.1 MB ($\le 1,536.0\text{ MB}$ threshold) under historical test load. |
| **`GATE-VOICE-06`** | Disk Retention | **NOT RUN** | **BLOCKER**: 0 temp audio files observed; formal elevated kernel filesystem audit pending. |
| **`GATE-VOICE-07`** | Network Egress | **NOT RUN** | **BLOCKER**: 0 sockets observed; formal elevated kernel ETW DNS-client trace audit pending. |
| **`GATE-VOICE-08`** | Packaging Bounds | **NOT RUN** | Intermediate STT footprint (80.92 MB) is compliant; full subsystem deferred to Stage 4. |
| **`GATE-VOICE-09`** | Fail-Closed Safety | **PASS** | Passed on isolated prototype harness fixtures. Production adapter verification required. |
| **`GATE-VOICE-10`** | License Review | **NOT RUN** | STT tooling is MIT compliant; Piper GPL-3.0 vs. Sherpa Apache-2.0 deferred to Stage 4. |

### Concrete Blockers Preventing Stage 3 Promotion Today:
1. **Physical RAM Headroom Failure (`GATE-VOICE-05`)**: Historical benchmark load testing recorded an available RAM minimum of 863.1 MB, failing the $1,536.0\text{ MB}$ safety ceiling. Stage 3 cannot proceed until a clean benchmark run proves system RAM stability under concurrent development and Ollama workloads.
2. **GPU LLM Co-Residency (`GATE-VOICE-04`)**: During preflight inspection, Ollama reported 0 resident models. Under `AGENTS.md` hardware rules, automated model warmup or pulling is prohibited. A physical test with an owner-warmed `qwen3.5:2b` resident model must prove $\Delta\text{VRAM} = 0\text{ MiB}$ and same-artifact digest continuity.
3. **STT Accuracy Verification (`GATE-VOICE-02`)**: Pending owner decision on Option A vs. Option B and subsequent measurement.
4. **Elevated Privacy Audits (`GATE-VOICE-06` & `GATE-VOICE-07`)**: Kernel ETW DNS and minifilter filesystem monitoring require administrative elevation.
5. **Production Safety Boundary**:
   - `LocalWhisperSpeechToTextAdapter` in `pico/perception/audio/adapter.py` remains strictly `is_available() == False`.
   - Production speech recognition fails closed immediately.

---

## 6. Decisions Required from Project Owner

To progress from this proposal to normative document amendments, the project owner must review and decide upon the following four explicit questions:

### Owner Decision Checklist

- [ ] **Decision 1: Stage-Specific Gate Decomposition**
  - **Proposed**: Formally decouple P11.3 into Stage 3 (STT Gates 01, 02, 04, 05, 06, 07, 08-STT, 09, STT-License) and Stage 4 (TTS Gates 03, 08-Full, 10, and Combined Voice Re-Verification).
  - *Options*: `APPROVE Proposed Decomposition` | `REJECT (Retain Unified 10-Gate Blocker Matrix)` | `REVISE (Specify Adjustments)`

- [ ] **Decision 2: `GATE-VOICE-02` Accuracy Corpus Acceptance Standard**
  - **Proposed**: Adopt **Option B** (require naturally spoken human recordings of the four canonical command texts for formal accuracy qualification; retain synthetic SAPI clips for automated CI regression).
  - *Options*: `APPROVE Option B (Human Command Audio)` | `APPROVE Option A (Synthetic SAPI Audio)` | `REVISE (Specify Alternative)`

- [ ] **Decision 3: `GATE-VOICE-08` Packaging Footprint Treatment**
  - **Proposed**: Treat `GATE-VOICE-08` at Stage 3 as an intermediate check ($\le 150\text{ MB}$ STT slice, severity `HIGH`), and at Stage 4 as a mandatory release packaging requirement ($< 350\text{ MB}$ combined voice subsystem).
  - *Options*: `APPROVE Proposed Treatment` | `ELEVATE to BLOCKER at all stages` | `REVISE (Specify Adjustments)`

- [ ] **Decision 4: Authorization to Amend Normative Architecture Documents**
  - **Proposed**: Upon owner decision on items 1–3, author formal amendments to [ADR 0005](0005-local-voice-engine-selection-and-hardware-qualification.md), [Qualification Protocol](../evaluation/voice-engine-qualification.md), and [Implementation Plan](../implementation-plan.md).
  - *Options*: `AUTHORIZE Document Amendments` | `DEFER Amendments`

---

## 7. Next Steps & Implementation Sequence (Post-Approval)

Following project owner approval of this proposal:

1. **Phase 1: Normative Document Amendments**:
   - Amend ADR 0005 to record the accepted stage-specific promotion gates and corpus interpretation.
   - Update `docs/evaluation/voice-engine-qualification.md` §7 to formalize the separate Stage 3 and Stage 4 gate matrices.
   - If Option B is approved, update `docs/evaluation/voice_corpus_manifest.json` to define the ingestion schema for human command recordings.
2. **Phase 2: Pre-Qualification Execution (Stage 2 Closure)**:
   - Procure or record the approved command audio fixtures.
   - Run the benchmark harness with resident Ollama LLM to resolve `GATE-VOICE-04` (VRAM) and measure `GATE-VOICE-02` (Accuracy).
   - Re-measure system RAM headroom under controlled conditions to remediate `GATE-VOICE-05`.
   - Conduct elevated privacy audits to close `GATE-VOICE-06` (Disk) and `GATE-VOICE-07` (Network).
3. **Phase 3: Stage 3 Runtime Implementation**:
   - Only after all Stage 3 promotion gates are physically passed and verified in `docs/evaluation/voice-qualification-results.md`, begin implementing `LocalWhisperSpeechToTextAdapter` in `pico.perception.audio.adapter`.
