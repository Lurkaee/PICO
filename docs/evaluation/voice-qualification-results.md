# PICO — Local STT (whisper.cpp) Hardware Qualification Report (Stage 2 Prototype)

- **Date**: 2026-11-17T18:41:05.170216+00:00
- **Milestone**: P11.3.2 Stage 2 Native Whisper Qualification Prototype
- **Status**: Stage 2 Prototype — Partial Qualification (Core Latency & RAM Met; Ollama Co-Residency, Accuracy, Egress & Disk Retention NOT RUN)
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
| `GATE-VOICE-01` | **STT Latency** | RTF <= 0.30 (5s audio in <= 1.5s) | 5s sample 'audio_cmd_workflow_5s.wav': End-to-End P50=1.1432s (RTF=0.2324), P90=1.2088s, P95=1.2088s, Max=1.2088s; Native-only P50=1.1273s (RTF=0.2291), P90=1.1925s, P95=1.1925s, Max=1.1925s [Breakdown: WAV parse=0.07ms, PCM norm=2.73ms, C buf=7.56ms, Native=1128.26ms] | **PASS** |
| `GATE-VOICE-02` | **STT Accuracy (WER)** | Aggregate WER <= 12.0% on representative multi-speaker real speech corpus | Preliminary real-speech aggregate WER=2.67% (Macro WER=8.33%, CER=6.41%). Smoke command WER=0.0%. Qualification marked NOT RUN pending multi-speaker conversational corpus per manifest. | NOT RUN |
| `GATE-VOICE-03` | **TTS Latency** | TTFAC < 500ms; RTF_synth <= 0.15 | Deferred to Milestone P11.3.4 (Piper/sherpa-onnx qualification) | NOT RUN |
| `GATE-VOICE-04` | **Single-Model GPU VRAM** | VRAM Delta = 0 MiB (preserves Ollama LLM residency) | CPU isolation component PASS (0 MiB additional VRAM; initial=162 MiB, peak=162 MiB, final=162 MiB across 102 observations). Single-model residency qualification NOT RUN because target model 'qwen3.5:2b' was not resident (Ollama local API unreachable (http://127.0.0.1:11434): <urlopen error timed out>). | NOT RUN |
| `GATE-VOICE-05` | **System RAM Ceiling** | Peak Working Set < 450 MB and Available System RAM > 1.5 GB | Peak Working Set: 328.75 MB (< 450.0 MB), Min Available System RAM: 2073.5 MB (> 1.5 GB), Private Bytes: 808.8 MB | **PASS** |
| `GATE-VOICE-06` | **Disk Retention** | 0 raw-audio bytes persisted to disk | 0 raw audio files created in process temp/working directories; independent kernel ETW minifilter filesystem audit unavailable without administrative elevation | NOT RUN |
| `GATE-VOICE-07` | **Network Egress** | 0 outbound socket connections & 0 DNS queries | 0 active socket connections detected for PID 25196; kernel ETW DNS-client activity tracing unavailable without elevation | NOT RUN |
| `GATE-VOICE-08` | **Packaging Bounds** | Combined voice subsystem footprint < 350 MB | STT Prototype: 80.92 MB (Binaries: 2.92 MB, Model: 77.99 MB). Combined STT+TTS voice subsystem gate deferred to Milestone P11.3.4. | NOT RUN |
| `GATE-VOICE-09` | **Fail-Closed Safety** | Deterministic typed exception on missing or corrupted artifact | Verified: missing model fails closed, corrupted model fails closed, missing runtime fails closed in isolated fixtures | **PASS** |
| `GATE-VOICE-10` | **License Review** | Permissive MIT license without copyleft risk (voice subsystem full qualification) | STT Prototype Component License Assessment: PASS (whisper.cpp engine MIT, OpenAI Whisper weights MIT). Overall GATE-VOICE-10 NOT RUN pending Milestone P11.3.4 TTS engine and voice-model licensing qualification. | NOT RUN |

---

## 2. Benchmark Results Across Test Corpus

| Audio Sample | Type | Duration | E2E P50 | Native P50 | E2E P90 | E2E P95 | E2E Max | E2E RTF | Native RTF | WER (%) | CER (%) | Emitted Transcript |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `audio_cmd_dictation_10s.wav` | synthetic_smoke | 11.025s | 1.2914s | 1.2515s | 1.371s | 1.371s | 1.371s | **0.1171** | 0.1135 | 0.0% | 0.0% | "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically." |
| `audio_cmd_query_3s.wav` | synthetic_smoke | 3.405s | 1.117s | 1.1071s | 1.2068s | 1.2068s | 1.2068s | **0.3281** | 0.3251 | 0.0% | 0.0% | "What tasks are currently waiting for my approval?" |
| `audio_cmd_short_1s.wav` | synthetic_smoke | 1.775s | 1.0489s | 1.0438s | 1.1349s | 1.1349s | 1.1349s | **0.5909** | 0.5881 | 0.0% | 0.0% | "Pico Stop" |
| `audio_cmd_workflow_5s.wav` | synthetic_smoke | 4.92s | 1.1432s | 1.1273s | 1.2088s | 1.2088s | 1.2088s | **0.2324** | 0.2291 | 0.0% | 0.0% | "Summarize the latest system logs and create a new draft task for review." |
| `jfk.wav` | real_speech | 11.0s | 1.218s | 1.1806s | 1.2859s | 1.2859s | 1.2859s | **0.1107** | 0.1073 | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_dictation_10s.wav` | real_speech | 10.0s | 1.1945s | 1.1573s | 1.2378s | 1.2378s | 1.2378s | **0.1194** | 0.1157 | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_query_3s.wav` | real_speech | 3.2s | 1.081s | 1.0707s | 1.1269s | 1.1269s | 1.1269s | **0.3378** | 0.3346 | 0.0% | 0.0% | "And so, my fellow Americans!" |
| `jfk_short_1s.wav` | real_speech | 1.5s | 1.0563s | 1.0493s | 1.139s | 1.139s | 1.139s | **0.7042** | 0.6995 | 50.0% | 38.5% | "And so I felt" |
| `jfk_workflow_5s.wav` | real_speech | 5.1s | 1.1099s | 1.0918s | 1.1929s | 1.1929s | 1.1929s | **0.2176** | 0.2141 | 0.0% | 0.0% | "And so my fellow Americans, ask not!" |
| `vosk_digits.wav` | real_speech | 8.308s | 1.1479s | 1.118s | 1.3315s | 1.3315s | 1.3315s | **0.1382** | 0.1346 | 0.0% | 0.0% | "1 0 0 0 1 90210 01803" |

---

## 3. Accuracy Metrics Breakdown

- **Real Speech Aggregate WER**: 2.67% (total edits / total reference words)
- **Real Speech Macro-Average WER**: 8.33% (unweighted mean across clips)
- **Real Speech Macro-Average CER**: 6.41%
- **Synthetic Commands Smoke WER**: 0.0% (reported separately from real speech)

---

## 4. Hardware & Resource Telemetry

- **Cold Start Initialization Time**: 0.2087s (model mmap and tensor allocation)
- **Peak Working Set Memory**: 328.75 MB
- **Minimum Available System RAM**: 2073.5 MB (threshold > 1.5 GB)
- **Initial GPU VRAM**: 162 MiB
- **Peak GPU VRAM Observed**: 162 MiB (102 observations, sampled per iteration (~every 1s))
- **Final GPU VRAM**: 162 MiB
- **Maximum GPU VRAM Delta**: 0 MiB
- **Ollama LLM Co-Residency**: Ollama local API unreachable (http://127.0.0.1:11434): <urlopen error timed out>
- **Average Real-Time Factor (E2E RTF)**: **0.2896**

---

## 5. Architectural Invariants Verification

1. **Zero Raw-Audio Disk Retention**: Evaluated in-memory pointer pipeline. 0 audio files found in process temporary directories. Full OS kernel minifilter audit marked NOT RUN.
2. **Zero Cloud Egress**: Monitored socket connections for PID. Full kernel ETW DNS tracing marked NOT RUN.
3. **In-Memory Buffer Zeroization**: Best-effort zeroization scrubs mutable application arrays (ctypes float arrays, lists, bytearrays). Immutable Python objects and native C++ heap buffers are freed upon context teardown.
4. **Fail-Closed Protection**: Verified immediate typed exceptions upon missing model, corrupted digest, and missing runtime in isolated fixtures.
5. **Production Boundary**: Production adapter (`LocalWhisperSpeechToTextAdapter`) remains strictly disabled (`is_available() == False`) and fails closed.
