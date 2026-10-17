# PICO Evaluation Framework

## 1. Overview

The PICO Evaluation Framework provides version-controlled, reproducible test cases to measure system correctness, model behavior, safety boundaries, latency, and resource consumption.

The suite is structured to expand from the initial 10 baseline cases to over 50 comprehensive benchmark scenarios.

---

## 2. Evaluation Case Schema

Each evaluation case is defined in versioned JSON format following this schema:

```json
{
  "id": "EVAL-CATEGORY-01",
  "title": "Short descriptive title",
  "category": "PERSISTENCE | INTENT | TOOL_SELECTION | TRUTHFULNESS | CONTEXT | RESILIENCE | POLICY | IDEMPOTENCY | PERFORMANCE | PRIVACY",
  "description": "Detailed explanation of what is tested",
  "input": {
    "prompt": "User query or synthetic trigger",
    "context": {},
    "data_classification": "PUBLIC | INTERNAL | SENSITIVE | SECRET"
  },
  "expected": {
    "behavior": "Expected system or model response",
    "assertions": ["Specific verifiable conditions"],
    "max_latency_ms": 500,
    "allowed_profiles": ["local-small", "cloud-strong"]
  },
  "evaluation_type": "DETERMINISTIC | HEURISTIC | MODEL_GRADED",
  "status": "ACTIVE"
}
```

---

## 3. Execution & Verification Conventions

- **Slice 1**: Evaluated deterministic persistence, API validation, foreign key constraints, and session isolation.
- **Slice 2**: Evaluated provider-agnostic gateway, SSE chat streaming, deterministic routing, and routing decision audit persistence.
- **Slice 3 (Current)**: Implemented local hardware benchmarking tool (`tools/bench/`), RAM/VRAM telemetry collectors, and preflight gates for `qwen3.5:2b`.
- **Reporting Invariant**: Cases must be reported strictly as `VERIFIED`, `FAILED`, `NOT TESTED`, or `BLOCKED BY MISSING CONFIGURATION`. Fabricated scores or telemetry are strictly prohibited.
- **Mock vs. Live Distinction**: A successful test run with `--mock` verifies runner plumbing, timing math, and file output. It must NEVER be reported as a completed hardware evaluation. A real hardware benchmark requires live Ollama inference with the target model loaded into RAM/VRAM.

---

## 4. Benchmark Runner Tool (`tools/bench/`)

PICO includes a CLI benchmark tool to measure model readiness and system resource consumption under the Windows target hardware baseline.

### Usage

```powershell
# Run deterministic mock benchmark (offline, no Ollama required)
.\.venv\Scripts\python.exe -m tools.bench.runner --mock --repetitions 3 --output-dir bench_reports

# Run live benchmark against local Ollama instance (requires Ollama running)
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 3 --output-dir bench_reports

# Run live benchmark with optional quality gates
.\.venv\Scripts\python.exe -m tools.bench.runner --model qwen3.5:2b --repetitions 5 --min-tokens-per-sec 15.0 --max-ttft-ms 1500.0
```

### Metrics Collected

| Metric | Source | Description |
| :--- | :--- | :--- |
| **Time to First Token (TTFT)** | Runner-measured | Milliseconds between request dispatch and arrival of the first token chunk. |
| **Generation Throughput** | Runtime / Runner | Generation speed in tokens per second (`eval_count / eval_duration_sec`). |
| **Prompt Processing Speed** | Runtime-reported | Context ingest speed in tokens per second (`prompt_eval_count / prompt_eval_duration_sec`). |
| **Total Duration** | Runner-measured | Wall-clock request completion duration in milliseconds. |
| **Model Load Duration** | Runtime-reported | Milliseconds spent by Ollama loading weights into memory before inference. |
| **System RAM Usage** | OS Kernel API (`GlobalMemoryStatusEx`) | Sampled host RAM footprint and process working set during generation. |
| **Dedicated GPU VRAM** | `nvidia-smi` | Dedicated VRAM allocated on the NVIDIA GPU (reported as unavailable if missing). |
| **Stream Integrity** | Runner-measured | Error rate and incomplete stream rate across iterations. |

### Artifacts

Reports are saved to `bench_reports/` (gitignored):
- Machine-readable JSON: `bench_<model>_<timestamp>.json`
- Human-readable Markdown summary: `bench_<model>_<timestamp>.md`
