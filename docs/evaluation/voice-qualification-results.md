# PICO — Local STT (whisper.cpp) Hardware Qualification Report

- **Date**: 2026-11-17T13:05:45.025758+00:00
- **Milestone**: P11.3.2 Stage 2 Native Whisper Qualification Prototype
- **Status**: Complete — Empirical Measurements Recorded
- **Host Hardware**: Intel64 Family 6 Model 186 Stepping 2, GenuineIntel on Windows-11-10.0.26300-SP0
- **Execution Target**: CPU-only (4 threads, AVX2 enabled, GPU disabled)
- **Runtime Engine**: whisper.cpp 1.9.5-dev
- **Model**: `ggml-base.en-q8_0.bin` (78.0 MB, SHA-256: `a4d4a0768075e13c...`)

---

## 1. Executive Summary & Qualification Gates

| Gate ID | Area / Gate | Required Threshold | Measured Result | Qualification Status |
|---|---|---|---|---|
| `GATE-VOICE-01` | **STT Latency** | RTF <= 0.30 (5s audio in <= 1.5s) | Avg RTF 0.5253, 5s sample: 1.1185s | FAIL |
| `GATE-VOICE-02` | **STT Accuracy (WER)** | WER <= 12.0% on real speech corpus | WER 23.08% (CER 6.41%) | FAIL |
| `GATE-VOICE-03` | **TTS Latency** | TTFAC < 500ms; RTF_synth <= 0.15 | Deferred to Stage 4 (Piper/sherpa-onnx qualification) | NOT RUN (STAGE 4) |
| `GATE-VOICE-04` | **Single-Model GPU VRAM** | VRAM Delta = 0 MiB (preserves LLM residency) | Delta 0 MiB | **PASS** |
| `GATE-VOICE-05` | **System RAM Ceiling** | Peak Working Set < 450 MB | Peak 314.74 MB (Delta 285.27 MB) | **PASS** |
| `GATE-VOICE-06` | **Disk Retention** | 0 raw-audio bytes written to disk | 0 bytes written (in-memory pointer pass) | **PASS** |
| `GATE-VOICE-07` | **Network Egress** | 0 outbound socket connections & 0 DNS queries | 0 sockets, 0 DNS calls | **PASS** |
| `GATE-VOICE-08` | **Packaging Bounds** | Combined footprint < 350 MB | Total 80.92 MB (Binaries: 2.9 MB, Model: 78.0 MB) | **PASS** |
| `GATE-VOICE-09` | **Fail-Closed Safety** | Immediate deterministic exception on missing/corrupted artifact | Verified (missing model, bad digest, missing DLL fail closed) | **PASS** |
| `GATE-VOICE-10` | **License Review** | Permissive MIT license without copyleft risk | whisper.cpp MIT; OpenAI Whisper MIT | **PASS** |

---

## 2. Benchmark Results Across Test Corpus

| Audio Sample | Type | Duration | P50 Latency | P90 Latency | RTF (P50) | WER (%) | CER (%) | Emitted Transcript |
|---|---|---|---|---|---|---|---|---|
| `audio_cmd_dictation_10s.wav` | synthetic_smoke | 11.025s | 5.3048s | 5.3048s | **0.4812** | 0.0% | 0.0% | "We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically." |
| `audio_cmd_query_3s.wav` | synthetic_smoke | 3.405s | 1.2195s | 1.2195s | **0.3582** | 0.0% | 0.0% | "What tasks are currently waiting for my approval?" |
| `audio_cmd_short_1s.wav` | synthetic_smoke | 1.775s | 1.0605s | 1.0605s | **0.5974** | 0.0% | 0.0% | "Pico Stop" |
| `audio_cmd_workflow_5s.wav` | synthetic_smoke | 4.92s | 1.1185s | 1.1185s | **0.2273** | 0.0% | 0.0% | "Summarize the latest system logs and create a new draft task for review." |
| `jfk.wav` | real_speech | 11.0s | 1.1784s | 1.1784s | **0.1071** | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_dictation_10s.wav` | real_speech | 10.0s | 1.2074s | 1.2074s | **0.1207** | 0.0% | 0.0% | "And so my fellow Americans, ask not what your country can do for you, ask what you can do for your country." |
| `jfk_query_3s.wav` | real_speech | 3.2s | 4.7175s | 4.7175s | **1.4742** | 0.0% | 0.0% | "And so, my fellow Americans!" |
| `jfk_short_1s.wav` | real_speech | 1.5s | 2.2734s | 2.2734s | **1.5156** | 38.5% | 38.5% | "And so I felt" |
| `jfk_workflow_5s.wav` | real_speech | 5.1s | 1.221s | 1.221s | **0.2394** | 0.0% | 0.0% | "And so my fellow Americans, ask not!" |
| `vosk_digits.wav` | real_speech | 8.308s | 1.0986s | 1.0986s | **0.1322** | 100.0% | 0.0% | "1 0 0 0 1 90210 01803" |

---

## 3. Hardware & Resource Telemetry

- **Cold Start Initialization Time**: 0.5176s (model mmap and tensor allocation)
- **Peak Working Set Memory**: 314.74 MB (Delta: 285.27 MB, well within <450 MB budget)
- **GPU Dedicated VRAM Delta**: 0 MiB (strictly 0 MiB, preserving Ollama LLM single-model residency)
- **Average Real-Time Factor (RTF)**: **0.5253** (Passing threshold: <= 0.30)
- **Real Speech Average WER**: **23.08%** (Passing threshold: <= 12.0%)

---

## 4. Architectural Invariants Verification

1. **Zero Raw-Audio Disk Retention**: Confirmed 0 raw audio files or temporary clips written to disk during capture or inference. PCM float samples reside strictly in RAM.
2. **Zero Cloud Egress**: Monitored socket connections confirmed 0 outbound connection attempts and 0 DNS queries attributable to the STT runtime.
3. **In-Memory Buffer Zeroization**: Verified that application-owned mutable float arrays are scrubbed in memory in `finally` blocks.
4. **Fail-Closed Protection**: Verified immediate typed exceptions upon missing DLL, missing model, or SHA-256 digest mismatches, preventing silent bypasses.
5. **Production Boundary**: The production adapter (`LocalWhisperSpeechToTextAdapter`) remains strictly disabled and fail-closed.