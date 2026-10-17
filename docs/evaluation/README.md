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

- **Slice 1 (Current)**: Evaluates deterministic persistence, API validation, foreign key constraints, and session isolation. Runs 100% offline with zero external model credentials required.
- **Slice 2 & 3 (Model Gateway & Benchmarks)**: Executes model-facing test cases against `local-small` (Ollama), `cloud-strong`, and the mock provider. Measures Time To First Token (TTFT), tokens/sec, and GPU/CPU memory.
- **Reporting Invariant**: Cases must be reported strictly as `VERIFIED`, `FAILED`, `NOT TESTED`, or `BLOCKED BY MISSING CONFIGURATION`. Fabricated scores are strictly prohibited.
