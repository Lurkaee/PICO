# PICO — Local STT (whisper.cpp) Hardware Qualification Report (Stage 2 Prototype)

- **Date**: 2026-11-17T16:40:31.031883+00:00
- **Milestone**: P11.3.2 Stage 2 Native Whisper Qualification Prototype
- **Status**: Stage 2 Prototype — Partial Qualification (Core Latency & VRAM Met; Accuracy, Egress & Disk Retention NOT RUN)
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
| `GATE-VOICE-01` | **STT Latency** | RTF <= 0.30 (5s audio in <= 1.5s) | 5s sample 'audio_cmd_workflow_5s.wav': P50=1.1472s (RTF=0.2332), P90=1.3277s, P95=1.3277s, Max=1.3277s | **PASS** |
| `GATE-VOICE-02` | **STT Accuracy (WER)** | Aggregate WER <= 12.0% on representative multi-speaker real speech corpus | Preliminary real-speech aggregate WER=2.67% (Macro WER=8.33%, CER=6.41%). Smoke command WER=0.0%. Qualification marked NOT RUN pending multi-speaker conversational corpus per manifest. | NOT RUN |
| `GATE-VOICE-03` | **TTS Latency** | TTFAC < 500ms; RTF_synth <= 0.15 | Deferred to Milestone P11.3.4 (Piper/sherpa-onnx qualification) | NOT RUN |
| `GATE-VOICE-04` | **Single-Model GPU VRAM** | VRAM Delta = 0 MiB (preserves Ollama LLM residency) | VRAM Delta = 0 MiB (CPU isolation verified). LLM concurrent residency NOT RUN (Ollama local API unreachable (http://127.0.0.1:11434): <urlopen error timed out>) | **PASS** |
| `GATE-VOICE-05` | **System RAM Ceiling** | Peak Working Set < 450 MB | Peak Working Set: 330.32 MB (Private: 803.51 MB, System Avail RAM: 1138.0 MB) | **PASS** |
| `GATE-VOICE-06` | **Disk Retention** | 0 raw-audio bytes persisted to disk | 0 raw audio files created in process temp/working directories; independent kernel ETW minifilter filesystem audit unavailable without administrative elevation | NOT RUN |
| `GATE-VOICE-07` | **Network Egress** | 0 outbound socket connections & 0 DNS queries | 0 active socket connections detected for PID 23156; kernel ETW DNS-client activity tracing unavailable without elevation | NOT RUN |
| `GATE-VOICE-08` | **Packaging Bounds** | Combined voice subsystem footprint < 350 MB | STT Prototype: 80.92 MB (Binaries: 2.92 MB, Model: 77.99 MB). Combined STT+TTS voice subsystem gate deferred to Milestone P11.3.4. | NOT RUN |
| `GATE-VOICE-09` | **Fail-Closed Safety** | Deterministic typed exception on missing or corrupted artifact | Verified: missing model fails closed, corrupted model fails closed, missing runtime fails closed in isolated fixtures | **PASS** |
| `GATE-VOICE-10` | **License Review** | Permissive MIT license without copyleft risk | STT Prototype: whisper.cpp engine (MIT), OpenAI Whisper weights (MIT). TTS licensing and voice model lineage deferred to Stage 4. | **PASS** |

---

## 2. Benchmark Results Across Test Corpus

| Audio Sample | Type | Duration | P50 Latency | P90 Latency | P95 Latency | Max Latency | RTF (P50) | WER (%) | CER (%) | Emitted Transcript |
|---|---|---|---|---|---|---|---|---|---|---|
| `audio_cmd_dictation_10s.wav` | synthetic_smoke | 11.025s | 1.2166s | 1.3491s | 1.3491s | 1.3491s | **0.1104** | 0.0% | 0.0% | "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically." |
| `audio_cmd_query_3s.wav` | synthetic_smoke | 3.405s | 1.3234s | 1.6473s | 1.6473s | 1.6473s | **0.3887** | 0.0% | 0.0% | "What tasks are currently waiting for my approval?" |
| `audio_cmd_short_1s.wav` | synthetic_smoke | 1.775s | 1.0895s | 1.1472s | 1.1472s | 1.1472s | **0.6138** | 0.0% | 0.0% | "Pico Stop" |
| `audio_cmd_workflow_5s.wav` | synthetic_smoke | 4.92s | 1.1472s | 1.3277s | 1.3277s | 1.3277s | **0.2332** | 0.0% | 0.0% | "Summarize the latest system logs and create a new draft task for review." |
| `jfk.wav` | real_speech | 11.0s | 1.1668s | 1.3577s | 1.3577s | 1.3577s | **0.1061** | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_dictation_10s.wav` | real_speech | 10.0s | 1.2901s | 1.5148s | 1.5148s | 1.5148s | **0.129** | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_query_3s.wav` | real_speech | 3.2s | 1.0516s | 1.177s | 1.177s | 1.177s | **0.3286** | 0.0% | 0.0% | "And so, my fellow Americans!" |
| `jfk_short_1s.wav` | real_speech | 1.5s | 1.0962s | 1.1369s | 1.1369s | 1.1369s | **0.7308** | 50.0% | 38.5% | "And so I felt" |
| `jfk_workflow_5s.wav` | real_speech | 5.1s | 1.1342s | 1.1911s | 1.1911s | 1.1911s | **0.2224** | 0.0% | 0.0% | "And so my fellow Americans, ask not!" |
| `vosk_digits.wav` | real_speech | 8.308s | 1.3471s | 1.4553s | 1.4553s | 1.4553s | **0.1621** | 0.0% | 0.0% | "1 0 0 0 1 90210 01803" |

---

## 3. Accuracy Metrics Breakdown

- **Real Speech Aggregate WER**: 2.67% (total edits / total reference words)
- **Real Speech Macro-Average WER**: 8.33% (unweighted mean across clips)
- **Real Speech Macro-Average CER**: 6.41%
- **Synthetic Commands Smoke WER**: 0.0% (reported separately from real speech)

---

## 4. Hardware & Resource Telemetry

- **Cold Start Initialization Time**: 0.1953s (model mmap and tensor allocation)
- **Peak Working Set Memory**: 330.32 MB
- **GPU Dedicated VRAM Delta**: 0 MiB
- **Ollama LLM Co-Residency**: Ollama local API unreachable (http://127.0.0.1:11434): <urlopen error timed out>
- **Average Real-Time Factor (RTF)**: **0.3025**

---

## 5. Architectural Invariants Verification

1. **Zero Raw-Audio Disk Retention**: Evaluated in-memory pointer pipeline. 0 audio files found in process temporary directories. Full OS kernel minifilter audit marked NOT RUN.
2. **Zero Cloud Egress**: Monitored socket connections for PID. Full kernel ETW DNS tracing marked NOT RUN.
3. **In-Memory Buffer Zeroization**: Best-effort zeroization scrubs mutable application arrays (ctypes float arrays, lists, bytearrays). Immutable Python objects and native C++ heap buffers are freed upon context teardown.
4. **Fail-Closed Protection**: Verified immediate typed exceptions upon missing model, corrupted digest, and missing runtime in isolated fixtures.
5. **Production Boundary**: Production adapter (`LocalWhisperSpeechToTextAdapter`) remains strictly disabled (`is_available() == False`) and fails closed.
