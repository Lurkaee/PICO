# 0006. Architecture Decision Proposal: P11.3 Voice Qualification Promotion Rules & Corpus Interpretation

- **Date**: 2026-11-18 (Updated 2026-11-19)
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

This proposal addresses three structural ambiguities and canonical inconsistencies identified during the closure of Milestone P11.3.2 Stage 2 and recorded in PR #94:
1. **The Stage 3 / Stage 4 Qualification Scope Ambiguity**: The tension between the sequential implementation plan (which separates STT Stage 3 from TTS Stage 4) and the qualification protocol §7 (which lists a unified set of ten gates as blockers before Stage 3/4).
2. **The `GATE-VOICE-02` Accuracy Corpus Acceptance Interpretation**: The tension between the ADR requirement for "conversational English commands", the protocol specification of four named command clips, and the reality that those clips in the current manifest are synthetic Windows SAPI recordings.
3. **The Voice Working Set RAM Threshold Inconsistency**: The variation between ADR 0005 §4.1 (< 450 MB), ADR 0005 §6 (< 500 MB), and Qualification Protocol §7 (< 450 MB peak with > 1.5 GB free RAM).

Crucially, this proposal establishes a strict distinction across the five sequential lifecycle phases of Milestone P11.3:
- **Step A — Owner Decision & Normative Approval**: Owner decisions must be incorporated into normative documents via a reviewed, merged amendment PR before any implementation authorization.
- **Step B — Stage 2 Qualification Closure**: Hardware pre-qualification gates (`GATE-VOICE-02`, `04`, `05`, `06`, `07`) must be satisfied on host hardware. Owner approval alone cannot bypass Stage 2 qualification.
- **Step C — Stage 3 Development Authorization**: Adapter implementation begins only after normative amendments are merged and Stage 2 pre-qualification is closed. The adapter remains disabled (`is_available() == False`) and fails closed.
- **Step D — Integrated Adapter & Privacy Qualification**: Re-verifies privacy and fail-closed safety on the completed production adapter and capture pipeline (`GATE-VOICE-06` zero disk retention & buffer zeroization, `GATE-VOICE-07` zero network egress, `GATE-VOICE-09` typed fail-closed safety) prior to activation.
- **Step E — Production Activation**: Enabling production STT (`is_available() == True`) requires verifying all Stage 3 conditions (Step B baseline evidence accepted, Step D integrated privacy and fail-closed checks passed, all approved Stage 3 gates, and STT component licensing under ADR 0005 §2.7).

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

### 2.3 Conflict 3: Canonical Voice Working Set RAM Threshold Inconsistency

An explicit numerical inconsistency exists across canonical project documents regarding peak memory allocation:

| Canonical Document Reference | Stated Working Set Target | Stated System Free RAM Headroom | Context |
| :--- | :--- | :--- | :--- |
| **ADR 0005 §4.1 (Table 4.1)** | `< 450 MiB Working Set` | `~1,500 MiB` (above free threshold) | Voice STT/TTS Allocation Target |
| **ADR 0005 §6 (Table 6, Row 1)** | `< 500 MB total peak Working Set` | Not explicitly listed in row | Runtime Memory Target across whisper.cpp and Piper |
| **Qualification Protocol §7 (`GATE-VOICE-05`)** | `Voice peak Working Set < 450 MB` | `System Free RAM > 1.5 GB` ($> 1,536.0\text{ MB}$) | Formal Pass/Fail Blocker Gate |

- **Inconsistency Assessment**:
  - ADR 0005 §4.1 and Protocol §7 specify a **$450\text{ MB}$ Working Set ceiling**, while ADR 0005 §6 specifies a **$500\text{ MB}$ peak Working Set target**.
  - Neither entry has formally superseded the other in accepted records.
- **Proposed Conservative Treatment Pending Owner Approval**:
  - **Stage 3 (STT Standalone)**: Voice STT peak Working Set strictly $< 450\text{ MB}$.
  - **Stage 4 (Combined STT + TTS)**: Combined peak Working Set strictly $< 450\text{ MB}$ (conservatively honoring the stricter Protocol §7 and ADR §4.1 threshold).
  - **System Available RAM**: Strictly $> 1,536.0\text{ MB}$ ($1.5\text{ GB}$) under concurrent development and Ollama workloads.
  - **Preserved Status**: `GATE-VOICE-05` remains **`FAIL`** based on historical test load dropping to 863.1 MB available RAM.

---

## 3. Proposed Stage-Specific Gate Matrix

To reconcile the sequential delivery model with rigorous hardware and safety gating, this proposal decomposes the unified ten-gate matrix into two decoupled, verifiable milestone stages.

Every canonical numerical threshold, severity classification, and protocol gate definition from Protocol §7 is strictly preserved.

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ PROPOSED STAGE-SPECIFIC PROMOTION DECOMPOSITION                                │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   STAGE 3: LOCAL STT RUNTIME PROMOTION GATES (Production Activation)            │
│   ├── GATE-VOICE-01 : STT Latency (RTF <= 0.30, 5s audio <= 1.5s)    [BLOCKER]  │
│   ├── GATE-VOICE-02 : STT Accuracy (WER <= 12.0% on command corpus)   [BLOCKER]  │
│   ├── GATE-VOICE-04 : Single-Model VRAM (Delta VRAM = 0 MiB, Ollama)  [BLOCKER]  │
│   ├── GATE-VOICE-05 : RAM Ceiling (Peak STT < 450 MB, Free > 1.5 GB)  [BLOCKER]  │
│   ├── GATE-VOICE-06 : Zero Audio Disk Retention & Buffer Zeroization  [BLOCKER]  │
│   ├── GATE-VOICE-07 : Zero Network & DNS Egress Isolation             [BLOCKER]  │
│   ├── GATE-VOICE-08 : Interim STT Packaging Measurement (Informational           │
│   │                   against canonical < 350 MB combined budget)     [HIGH]     │
│   ├── GATE-VOICE-09 : Integrated STT Fail-Closed Verification         [BLOCKER]  │
│   └── STT Licensing : Component-Level MIT License Verification                   │
│                       (Derived from ADR 0005 §2.7; non-numbered check)           │
│                                                                                 │
│   STAGE 4: LOCAL TTS RUNTIME PROMOTION GATES (Production Activation)            │
│   ├── GATE-VOICE-03 : TTS Latency (TTFAC < 500 ms, RTF_synth <= 0.15) [BLOCKER] │
│   ├── GATE-VOICE-08 : Combined Voice Subsystem Packaging (< 350 MB)   [HIGH]     │
│   │                   (Applies to release packaging & distribution)             │
│   ├── GATE-VOICE-10 : Complete Voice Subsystem License Review         [BLOCKER]  │
│   │                   (Piper GPL-3.0 Compatibility or Sherpa-ONNX)              │
│   └── Combined Voice Resource & Privacy Re-Verification:                        │
│       ├── Re-verify GATE-VOICE-04 (Co-residency with STT + TTS + LLM) [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-05 (Combined Working Set < 450 MB)     [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-06 (TTS Audio Memory Zeroization)      [BLOCKER]  │
│       ├── Re-verify GATE-VOICE-07 (TTS Zero Network/DNS Egress)       [BLOCKER]  │
│       └── Re-verify GATE-VOICE-09 (TTS Fail-Closed Safety)            [BLOCKER]  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

### 3.1 Stage 3 Promotion Matrix (Local Speech-to-Text Runtime Integration)

Before promoting PICO to **Milestone P11.3 Stage 3** (activating `LocalWhisperSpeechToTextAdapter` in production), the following gates and prerequisites must be satisfied:

| Gate / Evidence Item | Canonical Target Requirement | Severity | Verification Method | Stage 3 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-01`** | STT $\text{RTF} \le 0.30$ on Intel i5-13420H CPU (5s audio in $\le 1.5\text{s}$) | **BLOCKER** | Deterministic benchmark on `audio_cmd_workflow_5s.wav` | **Mandatory Precondition**. Passed historically ($\text{RTF}=0.2437$, P50=1.1992s). |
| **`GATE-VOICE-02`** | STT $\text{WER} \le 12.0\%$ on conversational English commands | **BLOCKER** | Word Error Rate evaluation across canonical command clips | **Mandatory Precondition**. Currently `NOT RUN`. Requires owner selection between Option A and Option B below. |
| **`GATE-VOICE-04`** | GPU $\Delta\text{VRAM} = 0\text{ MiB}$; single-model Ollama LLM residency preserved with zero eviction | **BLOCKER** | `nvidia-smi` telemetry with resident `qwen3.5:2b` | **Mandatory Precondition**. Currently `NOT RUN`. Requires pre-warmed Ollama LLM during test. |
| **`GATE-VOICE-05`** | STT peak Working Set $< 450\text{ MB}$; System Available RAM $> 1,536.0\text{ MB}$ under load | **BLOCKER** | Continuous memory sampler during simultaneous LLM + STT load | **Mandatory Precondition**. Currently `FAIL` (863.1 MB recorded under historical test load). |
| **`GATE-VOICE-06`** | Zero audio bytes written to persistent storage; mutable capture buffers zeroized | **BLOCKER** | Process file handle monitoring & buffer inspection | **Dual Verification Gate**. Step B host pre-qualification baseline; Step D integrated verification on production audio path before activation. Currently `NOT RUN` (verified in prototype; elevated kernel audit required). |
| **`GATE-VOICE-07`** | Zero outbound socket connections and zero DNS queries during transcription | **BLOCKER** | Socket inspection & DNS event monitoring during inference | **Dual Verification Gate**. Step B host pre-qualification baseline; Step D integrated verification on production audio path before activation. Currently `NOT RUN` (verified 0 sockets; elevated ETW trace required). |
| **`GATE-VOICE-08`** *(Interim Measurement)* | Total combined voice packaging $< 350\text{ MB}$. At Stage 3: interim footprint measurement of STT slice. | **HIGH** | Directory size check of STT bundle components | **Informational Interim Measurement**. Retains canonical `HIGH` severity. Measured prototype STT footprint is **80.92 MB** (informational evidence against eventual $< 350\text{ MB}$ budget; see §3.3). |
| **`GATE-VOICE-09`** | Deterministic typed exceptions on missing binary or corrupted model in integrated adapter | **BLOCKER** | Unit & integration tests asserting fail-closed behavior on the actual integrated adapter | **Integrated Qualification Gate**. Validated in prototype fixtures; must be re-verified on the implemented adapter before production activation (see §3.5). |
| **STT Component Licensing** *(Derived Prerequisite)* | Permissive open-source license (MIT) for Whisper runtime, shim, and GGML model | **Prerequisite** *(Non-numbered)* | Legal audit of `whisper.cpp` (MIT) and GGML weights | **Component-Level Prerequisite** derived from ADR 0005 §2.7. Prototype STT tooling is 100% MIT. Required for production activation (Step E). `GATE-VOICE-10` remains the formal full gate at Stage 4. |

---

### 3.2 Stage 4 Promotion Matrix (Local Text-to-Speech Runtime Integration)

Before promoting PICO to **Milestone P11.3 Stage 4** (activating `LocalTextToSpeechAdapter` in production), the remaining voice gates must be satisfied:

| Gate / Evidence Item | Canonical Target Requirement | Severity | Verification Method | Stage 4 Promotion Applicability |
| :--- | :--- | :--- | :--- | :--- |
| **`GATE-VOICE-03`** | TTS Time-to-First-Audio-Chunk (TTFAC) $< 500\text{ ms}$; Synthesis $\text{RTF}_{\text{synth}} \le 0.15$ | **BLOCKER** | High-resolution timer measuring synthesis on benchmark prompt corpus | **Mandatory Precondition for TTS**. Evaluates Piper ONNX or sherpa-onnx. |
| **`GATE-VOICE-08`** *(Combined Subsystem)* | Combined STT + TTS binaries, DLLs, and models disk footprint $< 350\text{ MB}$ total | **HIGH** | Installer staging check (`tools/packaging/stage_windows_bundle.py`) | **Release Packaging Gate**. Retains canonical `HIGH` severity. Applies to release packaging and distribution before Windows NSIS distribution (see §3.3). |
| **`GATE-VOICE-10`** | Complete voice subsystem licensing: resolution of Piper GPL-3.0 redistribution compatibility or adoption of `sherpa-onnx` (Apache 2.0) | **BLOCKER** | Formal legal & packaging architecture review sign-off | **Mandatory Precondition for Full Voice System**. Formal gate resolving copyleft redistribution in NSIS bundle. |
| **Combined Resource & Privacy Re-Verification** | Coexistence of STT + TTS + LLM: $\Delta\text{VRAM}=0\text{ MiB}$, Combined Working Set $< 450\text{ MB}$, Available RAM $> 1.5\text{ GB}$, zero disk retention, zero egress | **BLOCKER** | Comprehensive multi-modal benchmark executing simultaneous STT and TTS | **Mandatory Precondition for Voice System Completion**. Prevents resource regression when both voice runtimes are active. |

---

### 3.3 Precise Treatment of `GATE-VOICE-08` (Packaging Bounds)

To eliminate specification drift regarding packaging budgets:

1. **Canonical Budget Preservation**:
   - The accepted ADR 0005 §2.6 and Qualification Protocol §7 define exactly one packaging threshold:
     $$\text{Combined Voice Subsystem Footprint} < 350\text{ MB}$$
   - **No separate $\le 150\text{ MB}$ formal gate exists in normative records.** Any reference to a $150\text{ MB}$ threshold is an internal engineering heuristic and must not be treated as a formal gate.
2. **Preservation of Canonical Severity**:
   - `GATE-VOICE-08` is classified as **`HIGH`** in Qualification Protocol §7.
   - It is not silently elevated to `BLOCKER` in this proposal.
3. **Stage 3 Application (Informational Interim Measurement)**:
   - At Stage 3, only the STT runtime (`whisper.dll` / `pico_whisper_shim.dll` ~3.2 MB) and default STT model (`ggml-base.en-q8_0.bin` ~81.8 MB) are present, totaling **80.92 MB**.
   - This 80.92 MB footprint is recorded as **informational empirical evidence** demonstrating that STT consumes only ~23% of the eventual $350\text{ MB}$ allowance.
   - It does not constitute a separate pass/fail gate.
4. **Stage 4 Application (Release Packaging & Distribution Gate)**:
   - At Stage 4, the full voice subsystem (STT + Piper/Sherpa TTS + ONNX Runtime + voice models) is staged.
   - The combined bundle must satisfy the canonical $< 350\text{ MB}$ target as a release packaging requirement before distribution in the Windows NSIS installer.

---

### 3.4 Precise Classification of STT Licensing Evidence

To prevent proliferation of non-canonical gates:

1. **Component-Level Prerequisite, Not a New Gate**:
   - Verifying the licensing of `whisper.cpp`, the native MSVC shim, and GGML model weights is an **evidence prerequisite derived directly from ADR 0005 §2.7** (*"Licensing Compatibility"*).
   - It is **not** assigned a new numbered gate identifier (e.g. `GATE-VOICE-11`), nor does it invent a new severity classification.
2. **`GATE-VOICE-10` Remains the Canonical Gate**:
   - `GATE-VOICE-10` remains the formal, numbered qualification gate defined in Protocol §7: *"Resolution of Piper GPL-3.0 redistribution compatibility or adoption of sherpa-onnx (Apache 2.0)"*.
   - Because `GATE-VOICE-10` specifically addresses the copyleft redistribution implications of Piper TTS within the Windows NSIS package, it is formally scoped to **Stage 4**.
   - STT components (`whisper.cpp` MIT license) already comply with PICO's top-level MIT license, satisfying the component-level prerequisite for Stage 3 without ambiguity.

---

### 3.5 Operational Lifecycle: Reconciling Normative Approval, Stage 2 Closure, Development Authorization, Integrated Qualification, and Production Activation

To eliminate operational ambiguity between project planning and physical qualification execution, the lifecycle of Milestone P11.3 Stage 3 is structured into five distinct, sequential steps:

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│ FIVE-STEP OPERATIONAL LIFECYCLE FOR STAGE 3 PROMOTION                           │
├─────────────────────────────────────────────────────────────────────────────────┤
│                                                                                 │
│   STEP A: OWNER DECISION & NORMATIVE APPROVAL                                   │
│   └── Owner approves proposal; amendments incorporated into ADR 0005,           │
│       Qualification Protocol, and Manifest via separate merged PR.              │
│                                                                                 │
│   STEP B: STAGE 2 QUALIFICATION CLOSURE                                         │
│   └── Complete hardware pre-qualification on host machine:                      │
│       GATE-02 (Accuracy), GATE-04 (VRAM), GATE-05 (RAM under load),             │
│       GATE-06 (Disk retention), GATE-07 (Network egress).                        │
│       Remediate historical GATE-05 FAIL with valid empirical evidence.          │
│                                                                                 │
│   STEP C: STAGE 3 DEVELOPMENT AUTHORIZATION                                     │
│   └── Author LocalWhisperSpeechToTextAdapter in pico.perception.audio.adapter.  │
│       CRITICAL: Adapter remains disabled (is_available() == False) & fails closed│
│                                                                                 │
│   STEP D: INTEGRATED ADAPTER & PRIVACY QUALIFICATION                            │
│   └── Re-verify GATE-06 (Disk/Zeroize), GATE-07 (Zero Egress), and GATE-09      │
│       (Fail-Closed) against the implemented adapter and live capture path.      │
│                                                                                 │
│   STEP E: PRODUCTION ACTIVATION                                                 │
│   └── Require Step B baseline, Step D integrated privacy & fail-closed passes,  │
│       all Stage 3 gates, and STT licensing before is_available() == True.       │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

1. **Step A — Owner Decision and Normative Approval**:
   - The project owner reviews this proposal and decides upon the Stage 3/Stage 4 gate decomposition, corpus acceptance, RAM ceiling, and packaging treatment.
   - The approved decisions are formally incorporated into normative project documents ([ADR 0005](0005-local-voice-engine-selection-and-hardware-qualification.md), [Qualification Protocol](../evaluation/voice-engine-qualification.md), and [Voice Manifest](../evaluation/voice_corpus_manifest.json)) through a separate, reviewed, and merged PR.
   - **Crucial Boundary**: The proposal document itself is non-normative and never automatically alters accepted architecture baseline records.

2. **Step B — Stage 2 Qualification Closure**:
   - In accordance with `docs/implementation-plan.md`, Stage 2 qualification must be completed before Stage 3 implementation begins.
   - The applicable hardware pre-qualification gates must satisfy their approved Stage 3 requirements on target host hardware:
     - `GATE-VOICE-02` (STT Accuracy): Measured against the approved command corpus.
     - `GATE-VOICE-04` (Single-Model VRAM): Verified with resident Ollama LLM (`qwen3.5:2b`), proving $\Delta\text{VRAM} = 0\text{ MiB}$ and same-artifact digest continuity.
     - `GATE-VOICE-05` (RAM Ceiling): The historical `FAIL` (863.1 MB available RAM) must be remediated with valid new telemetry proving available system RAM $> 1,536.0\text{ MB}$ under concurrent load.
     - `GATE-VOICE-06` (Disk Retention) & `GATE-VOICE-07` (Network Egress): Verified via elevated kernel minifilter and ETW DNS trace audits.
   - **Strict Rule**: Owner approval alone does **not** bypass Stage 2 qualification. Thresholds cannot be softened, passes cannot be inferred from idle preflights, and unmeasured audits must remain `NOT RUN`.

3. **Step C — Stage 3 Development Authorization**:
   - Only after normative amendments are merged (Step A) **and** applicable Stage 2 pre-qualification prerequisites are satisfied and recorded (Step B), implementation of `LocalWhisperSpeechToTextAdapter` in `pico.perception.audio.adapter` is authorized to begin.
   - This includes authoring the adapter class, wiring the in-memory C-ABI Whisper shim, implementing audio conversion, and adding deterministic unit tests.
   - **Safety Invariant**: Authorizing development does **NOT** authorize processing live production audio. During implementation, the adapter remains strictly disabled (`is_available() == False`) and fails closed with `ValidationError("Speech transcription runtime is unavailable")`.

4. **Step D — Integrated Adapter & Privacy Qualification (`GATE-VOICE-06`, `GATE-VOICE-07`, `GATE-VOICE-09`)**:
   - Step D exercises the real, implemented `LocalWhisperSpeechToTextAdapter` class and its production audio capture/transcription pipeline, rather than relying solely on isolated prototype fixtures:
     1. **`GATE-VOICE-06` (Integrated Disk Retention & Buffer Zeroization)**: Verify that the integrated capture and transcription path writes zero raw-audio bytes to persistent storage (audited via file handle monitoring and kernel minifilter inspection) and that mutable capture buffers are deterministically zeroized.
     2. **`GATE-VOICE-07` (Integrated Network & DNS Egress Isolation)**: Verify zero prohibited outbound network connections and zero DNS queries during active transcription on the integrated pipeline (audited via socket inspection and ETW DNS tracing).
     3. **`GATE-VOICE-09` (Integrated Fail-Closed Verification)**: Verify typed fail-closed behavior (`ValidationError("Speech transcription runtime is unavailable")`) upon missing DLLs, missing/corrupted models, and malformed audio buffers directly against the implemented adapter class.
   - **Preservation of Step B Baseline**: Step B hardware pre-qualification establishes host-level baseline compliance. Step D is an additional, mandatory integration-level safety verification on the actual production code, not a replacement for Step B.

5. **Step E — Production Activation**:
   - Flipping `LocalWhisperSpeechToTextAdapter.is_available()` from `False` to `True` is the final, consequential step under Policy A authorization.
   - Production activation is strictly prohibited until **every Stage 3 promotion condition** has been physically verified, recorded, and accepted in `docs/evaluation/voice-qualification-results.md`:
     1. `Step B Baseline Evidence Accepted`: Stage 2 hardware pre-qualification prerequisites satisfied (`GATE-VOICE-02` $\text{WER} \le 12.0\%$, `GATE-VOICE-04` $\Delta\text{VRAM} = 0\text{ MiB}$, `GATE-VOICE-05` available RAM $> 1,536.0\text{ MB}$, baseline `GATE-VOICE-06`, baseline `GATE-VOICE-07`).
     2. `GATE-VOICE-01`: STT Latency ($\text{RTF} \le 0.30$, 5s audio in $\le 1.5\text{s}$) — **PASS**
     3. `GATE-VOICE-06 (Integrated)`: Zero Raw-Audio Disk Retention & Buffer Zeroization on production audio pipeline — **PASS**
     4. `GATE-VOICE-07 (Integrated)`: Zero Outbound Network & DNS Egress Isolation on production audio pipeline — **PASS**
     5. `GATE-VOICE-08 (Interim)`: Interim STT Packaging Measurement ($< 350\text{ MB}$ overall allowance) — **INFORMATIONAL (HIGH)**
     6. `GATE-VOICE-09 (Integrated)`: Integrated Adapter Fail-Closed Verification — **PASS**
     7. `STT Component Licensing`: Permissive MIT license verification under ADR 0005 §2.7 — **VERIFIED**

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
│   ├── Scope: Proves recognition of synthetic SAPI speech only.                  │
│   └── Strength: 100% reproducible, zero acoustic variance, already staged.      │
│                                                                                 │
│   OPTION B: CANONICAL HUMAN RECORDING BENCHMARK (Recommended)                   │
│   ├── Formal Gate: Record the exact text of the 4 canonical commands spoken     │
│   │   by a natural human voice through a real microphone.                       │
│   ├── Target: WER <= 12.0% on the natural human command recordings.             │
│   ├── Demographic Quotas: Explicitly rejected (no 5-10 speaker requirement).    │
│   ├── SAPI Testing: Retained as automated CI regression fixtures.               │
│   ├── Scope: Empirical proof for the 4 canonical commands, recorded speaker,    │
│   │          tested microphone, and pinned STT model/runtime only.              │
│   └── Strength: Eliminates false confidence of synthetic loopback audio.         │
│                                                                                 │
└─────────────────────────────────────────────────────────────────────────────────┘
```

#### Option A: Accept Deterministic SAPI-Generated Command Samples for Formal Protocol
- **Specification**:
  - Accept the four existing synthetic Windows SAPI command clips (`audio_cmd_short_1s.wav`, `audio_cmd_query_3s.wav`, `audio_cmd_workflow_5s.wav`, `audio_cmd_dictation_10s.wav`) as the authoritative corpus for formal `GATE-VOICE-02` qualification.
  - If measured $\text{WER} \le 12.0\%$ on these clips, `GATE-VOICE-02` is marked **PASS**.
  - Naturally spoken human audio recordings (e.g. JFK inaugural speech slices, supplemental operator recordings) are treated strictly as non-blocking, informational telemetry.
- **Required Normative Amendments if Option A is Approved**:
  1. Preserve the existing SAPI audio fixtures as the formal deterministic benchmark corpus in `docs/evaluation/voice_corpus_manifest.json`.
  2. Explicitly document in Qualification Protocol §3.1.2 and results that formal accuracy qualification applies strictly to synthetic Windows SAPI speech.
  3. Reconcile the manifest's existing qualification scope limitation with the approved formal criterion.
  4. Retain naturally spoken human evaluation as supplementary, non-blocking benchmarking.
  5. State explicitly that synthetic speech qualification does not establish human speech recognition accuracy.
  6. Preserve the canonical threshold: $\text{WER} \le 12.0\%$. Gate remains `NOT RUN` until formally evaluated.
- **Trade-Off Analysis**:
  - *Advantages*: Completely deterministic, reproducible across CI, zero acoustic variance, already staged in repository.
  - *Disadvantages*: SAPI synthetic voices have rigid cadence and unnatural spectral envelopes. Passing provides zero empirical evidence regarding human speech recognition.

#### Option B: Require Naturally Spoken Human Recordings of Canonical Commands (RECOMMENDED)
- **Specification**:
  - Formal qualification of `GATE-VOICE-02` requires recording the **exact text of the four canonical commands** spoken by a natural, consenting human operator through a physical microphone under standard room acoustic conditions.
  - In accordance with the removal of specification drift, **no arbitrary demographic or multi-speaker minimum (e.g. 5–10 speakers across specific genders/accents) is required**. Recording the four canonical human command texts with valid provenance by a consenting human speaker satisfies the proposed corpus-input requirement, but does **not** automatically pass `GATE-VOICE-02`. `GATE-VOICE-02` remains `NOT RUN` until the approved corpus is actually benchmarked on host hardware. It becomes **PASS** only if measured $\text{WER} \le 12.0\%$; if measured $\text{WER} > 12.0\%$, the gate is **FAIL**.
  - The four existing SAPI-generated clips are retained in the test suite as deterministic smoke-test fixtures for automated CI runs, but cannot satisfy formal `GATE-VOICE-02` qualification.
- **Precise Evidentiary Scope**:
  - This benchmark provides empirical evidence strictly for:
    1. The four canonical command texts.
    2. The actual recorded speaker.
    3. The tested physical microphone and acoustic recording environment.
    4. The measured STT model weights (`base.en-q8_0`) and native runtime implementation.
  - **Explicit Non-Claims**: Passing this corpus does **NOT** prove general speech recognition reliability across diverse speakers, unconstrained acoustic environments, regional accents, or arbitrary conversational spoken commands. Its value lies in eliminating the false confidence of synthetic text-to-speech audio by testing genuine human acoustic phonemes on the exact canonical command texts.
- **Required Normative Amendments if Option B is Approved**:
  1. Ingest naturally spoken human recordings of the four canonical command texts into `docs/evaluation/voice_corpus_manifest.json` with full provenance, SHA-256 digests, audio format (16 kHz 16-bit mono signed PCM), speaker/session metadata, and verified reference transcripts.
  2. Update Qualification Protocol §3.1.2 to reference the approved human recordings as the formal accuracy corpus.
  3. Retain the existing Windows SAPI clips as deterministic synthetic smoke-test fixtures, clarifying that they do not constitute formal human speech accuracy evidence.
  4. Revise the manifest's qualification scope limitation to accurately describe the accepted formal evaluation scope (single consenting speaker command evaluation) without obsolete multi-speaker references.
  5. Maintain `GATE-VOICE-02` as `NOT RUN` until the approved human command corpus is physically benchmarked and valid empirical WER evidence meeting the $\le 12.0\%$ threshold is recorded.
- **Trade-Off Analysis**:
  - *Advantages*: Genuine empirical evidence on human speech on the canonical command texts; eliminates the false confidence of synthetic audio; strictly fulfills ADR 0005 §2.5 without inventing demographic quotas.
  - *Disadvantages*: Requires recording, verifying, and committing four new WAV files and manifest metadata.

---

### 4.2 Recommendation & Explicit Rationale

**The PICO Architecture & Engineering Team strongly recommends OPTION B.**

#### Technical & Architectural Rationale:
1. **Adherence to Core Principles (`AGENTS.md` Principles 1 & 10)**:
   - PICO is an autonomous personal intelligence coordinator, not a chatbot wrapper. Its speech perception capability directly determines whether spoken operator commands are faithfully transcribed into Policy A action proposals.
   - Principle 10 (*Truthful Reporting*) mandates that test outcomes reflect reality: claiming an STT engine is qualified for production based solely on transcribing synthetic Windows text-to-speech audio is fundamentally misleading.
2. **Alignment with ADR 0005 Intent**:
   - ADR 0005 §2.5 explicitly specifies *"conversational English commands"*. A synthetic speech synthesizer reading text is not a "conversational command"; it is an acoustic loopback artifact.
3. **Finite, Verifiable Scope**:
   - Option B does not require academic speech corpora or demographic sampling. It simply requires recording the four canonical command texts once with a real microphone. While this does not prove universal accent robustness, it provides genuine empirical confidence that the engine can process real human speech on the system's foundational command set.

---

## 5. Current Qualification Status & Why Stage 3 Remains Blocked

Regardless of which option the project owner selects, **Milestone P11.3 Stage 3 is currently NOT AUTHORIZED and remains strictly BLOCKED**.

The existing empirical qualification report ([docs/evaluation/voice-qualification-results.md](../evaluation/voice-qualification-results.md)) records the following verified baseline:

| Gate ID | Category | Status | Blocking Reason for Stage 3 Production Activation |
| :--- | :--- | :--- | :--- |
| **`GATE-VOICE-01`** | STT Latency | **PASS** | Historical benchmark passed (P50=1.1992s, $\text{RTF}=0.2437 \le 0.30$). Non-blocking. |
| **`GATE-VOICE-02`** | STT Accuracy | **NOT RUN** | **BLOCKER**: Canonical command corpus accuracy is unmeasured with real human speech. |
| **`GATE-VOICE-03`** | TTS Latency | **NOT RUN** | Deferred to Stage 4 under the proposed stage-specific gate decomposition. |
| **`GATE-VOICE-04`** | Single-Model VRAM | **NOT RUN** | **BLOCKER**: Preflight showed 0 Ollama models resident; co-residency digest continuity unverified. |
| **`GATE-VOICE-05`** | RAM Ceiling | **FAIL** | **BLOCKER**: Available RAM dropped to 863.1 MB ($\le 1,536.0\text{ MB}$ threshold) under historical test load. |
| **`GATE-VOICE-06`** | Disk Retention | **NOT RUN** | **BLOCKER**: 0 temp audio files observed; formal elevated kernel filesystem audit pending. |
| **`GATE-VOICE-07`** | Network Egress | **NOT RUN** | **BLOCKER**: 0 sockets observed; formal elevated kernel ETW DNS-client trace audit pending. |
| **`GATE-VOICE-08`** | Packaging Bounds | **NOT RUN** | Interim STT footprint (80.92 MB) is recorded; full subsystem gate deferred to Stage 4. Severity: `HIGH`. |
| **`GATE-VOICE-09`** | Fail-Closed Safety | **PASS** | Passed on isolated prototype harness fixtures. Production adapter verification required. |
| **`GATE-VOICE-10`** | License Review | **NOT RUN** | STT tooling is MIT compliant; Piper GPL-3.0 vs. Sherpa Apache-2.0 deferred to Stage 4. |

### Concrete Blockers Preventing Stage 3 Promotion Today:
1. **Physical RAM Headroom Failure (`GATE-VOICE-05`)**: Historical benchmark load testing recorded an available RAM minimum of 863.1 MB, failing the $1,536.0\text{ MB}$ safety ceiling. Stage 3 cannot proceed until a clean benchmark run proves system RAM stability under concurrent development and Ollama workloads.
2. **GPU LLM Co-Residency (`GATE-VOICE-04`)**: During preflight inspection, Ollama reported 0 resident models. Under `AGENTS.md` hardware rules, automated model warmup or pulling is prohibited. A physical test with an owner-warmed `qwen3.5:2b` resident model must prove $\Delta\text{VRAM} = 0\text{ MiB}$ and same-artifact digest continuity.
3. **STT Accuracy Verification (`GATE-VOICE-02`)**: Pending owner decision on Option A vs. Option B, acquisition of the corresponding audio corpus, and physical benchmark evaluation.
4. **Elevated Privacy Audits (`GATE-VOICE-06` & `GATE-VOICE-07`)**: Kernel ETW DNS and minifilter filesystem monitoring require administrative elevation.
5. **Integrated Adapter Verification (`GATE-VOICE-09`)**: Testing fail-closed safety against the implemented production adapter class (Step D).
6. **STT Component Licensing Verification**: Confirming licensing compliance under ADR 0005 §2.7 (Step E).
7. **Stage 2 Qualification Closure**: Completion and acceptance of Step B prerequisites before Step C development begins.
8. **Production Safety Boundary**:
   - `LocalWhisperSpeechToTextAdapter` in `pico/perception/audio/adapter.py` remains strictly `is_available() == False`.
   - Production speech recognition fails closed immediately.

---

## 6. Decisions Required from Project Owner

To progress from this proposal to normative document amendments, the project owner must review and decide upon the following five explicit questions:

### Owner Decision Checklist

- [ ] **Decision 1: Stage-Specific Gate Decomposition**
  - **Proposed**: Formally decouple P11.3 into Stage 3 (STT Gates 01, 02, 04, 05, 06, 07, 08-interim, 09, STT licensing evidence) and Stage 4 (TTS Gates 03, 08-Full, 10, and Combined Voice Re-Verification).
  - *Options*: `APPROVE Proposed Decomposition` | `REJECT (Retain Unified 10-Gate Blocker Matrix)` | `REVISE (Specify Adjustments)`

- [ ] **Decision 2: `GATE-VOICE-02` Accuracy Corpus Acceptance Standard**
  - **Proposed**: Adopt **Option B** (require naturally spoken human recordings of the four canonical command texts for formal accuracy qualification; retain synthetic SAPI clips for automated CI regression).
  - *Options*: `APPROVE Option B (Human Command Audio)` | `APPROVE Option A (Synthetic SAPI Audio)` | `REVISE (Specify Alternative)`

- [ ] **Decision 3: `GATE-VOICE-08` Packaging Footprint Treatment**
  - **Proposed**: Preserve the canonical $< 350\text{ MB}$ total voice budget and `HIGH` severity. At Stage 3, record STT footprint (80.92 MB) as an informational interim measurement without inventing a separate 150 MB gate. At Stage 4, evaluate the combined voice subsystem against $< 350\text{ MB}$ as a release packaging requirement.
  - *Options*: `APPROVE Proposed Treatment` | `ELEVATE to BLOCKER at Stage 4` | `REVISE (Specify Adjustments)`

- [ ] **Decision 4: Working Set RAM Inconsistency Resolution (`GATE-VOICE-05`)**
  - **Proposed**: Resolve the inconsistency between ADR 0005 §4.1 (450 MB) and ADR 0005 §6 (500 MB) by adopting the conservative `< 450 MB` peak Working Set limit for both Stage 3 STT and Stage 4 combined voice, while strictly enforcing $> 1,536.0\text{ MB}$ available system RAM headroom.
  - *Options*: `APPROVE Conservative 450 MB Ceiling` | `ACCEPT 500 MB Table 6 Limit` | `REVISE (Specify Threshold)`

- [ ] **Decision 5: Authorization to Amend Normative Architecture Documents**
  - **Proposed**: Upon owner decision on items 1–4, author formal amendments to [ADR 0005](0005-local-voice-engine-selection-and-hardware-qualification.md), [Qualification Protocol](../evaluation/voice-engine-qualification.md), and [Implementation Plan](../implementation-plan.md).
  - *Options*: `AUTHORIZE Document Amendments` | `DEFER Amendments`

---

## 7. Next Steps & Implementation Sequence (Post-Approval)

Following project owner approval of this proposal, work proceeds in five strict phases aligned with the operational lifecycle (§3.5):

1. **Phase 1: Normative Document Amendments (Step A)**:
   - Amend ADR 0005 to record the accepted stage-specific promotion gates, corpus interpretation, and clarified RAM Working Set threshold.
   - Update `docs/evaluation/voice-engine-qualification.md` §7 to formalize the separate Stage 3 and Stage 4 gate matrices.
   - Update `docs/evaluation/voice-engine-qualification.md` §3.1.2 and `docs/evaluation/voice_corpus_manifest.json` according to the owner's choice:
     - **If Option B is approved**: Add naturally spoken human recordings of the four canonical command texts to the manifest with full provenance, SHA-256 digests, audio format, and reference transcripts; update Protocol §3.1.2 to identify these human recordings as the formal accuracy corpus; retain SAPI clips as deterministic smoke-test fixtures; and reconcile the manifest limitation note with the accepted single consenting speaker canonical evaluation scope.
     - **If Option A is approved**: Preserve existing SAPI clips as the formal deterministic corpus; document in Protocol §3.1.2 and results that formal accuracy applies to synthetic speech only; reconcile the manifest limitation note; and retain human evaluation as supplementary.
   - In both cases, preserve the canonical $\text{WER} \le 12.0\%$ target and keep `GATE-VOICE-02` as `NOT RUN` until physical benchmarking is completed.

2. **Phase 2: Stage 2 Hardware Pre-Qualification Closure (Step B)**:
   - Procure or record the approved command audio fixtures.
   - Run the benchmark harness with resident Ollama LLM to resolve `GATE-VOICE-04` (VRAM) and measure `GATE-VOICE-02` (Accuracy).
   - Re-measure system RAM headroom under controlled conditions to remediate `GATE-VOICE-05`.
   - Conduct elevated privacy audits to close `GATE-VOICE-06` (Disk) and `GATE-VOICE-07` (Network).
   - Document all verified empirical findings in `docs/evaluation/voice-qualification-results.md`.
   - **Strict Invariant**: Stage 2 must be formally closed before Stage 3 adapter implementation begins.

3. **Phase 3: Stage 3 Adapter Development Authorization (Step C)**:
   - Only after Phase 1 normative amendments are merged and Phase 2 pre-qualification prerequisites are satisfied, implementation of `LocalWhisperSpeechToTextAdapter` in `pico.perception.audio.adapter` is authorized.
   - Wire the in-memory C-ABI Whisper shim, implement 16 kHz 16-bit mono PCM conversion to float32, and implement buffer zeroization.
   - **Safety Invariant**: The production adapter remains strictly unavailable (`is_available() == False`) and fails closed during this phase. Implementation does not authorize live production audio processing.

4. **Phase 4: Integrated Adapter & Privacy Qualification (Step D)**:
   - Execute integrated safety and privacy verification directly against the implemented `LocalWhisperSpeechToTextAdapter` and its production audio capture/transcription pipeline:
     - `GATE-VOICE-06`: Confirm zero raw-audio bytes written to persistent storage and mutable buffer zeroization across the integrated transcription lifecycle.
     - `GATE-VOICE-07`: Confirm zero outbound socket connections and zero DNS queries during integrated transcription via socket inspection and ETW tracing.
     - `GATE-VOICE-09`: Confirm deterministic typed exceptions (`ValidationError`) on missing DLLs, missing/corrupted models, and malformed audio buffers.
   - **Baseline Continuity**: Step D verifies the live production adapter without weakening or replacing Step B's host-level pre-qualification baseline.

5. **Phase 5: Production Activation (Step E)**:
   - Only after Step B Stage 2 baseline evidence is accepted, Step D integrated privacy (`GATE-VOICE-06`, `07`) and fail-closed (`GATE-VOICE-09`) checks pass, `GATE-VOICE-01`, `02`, `04`, `05`, `08-interim` are satisfied, and STT component licensing under ADR 0005 §2.7 is verified and documented in `docs/evaluation/voice-qualification-results.md`, flip `LocalWhisperSpeechToTextAdapter.is_available()` to `True`.
