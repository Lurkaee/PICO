"""Synthetic, non-sensitive prompts and evaluation logic for PICO local model benchmark runner."""

from __future__ import annotations

import json
import re
from typing import NamedTuple


class BenchmarkPrompt(NamedTuple):
    prompt_id: str
    category: str
    prompt: str
    expected_output_hint: str
    max_tokens: int


WARMUP_PROMPT = BenchmarkPrompt(
    prompt_id="WARMUP-01",
    category="warmup",
    prompt="Respond with the single word 'READY'. Do not output any additional text.",
    expected_output_hint="READY",
    max_tokens=16,
)

SYNTHETIC_CONTEXT_DOCUMENT = (
    "=== SYSTEM ARCHITECTURE SPECIFICATION (SYNTHETIC BENCHMARK PAYLOAD) ===\n"
    "Deployment Target: Local Isolated Cluster\n"
    "Release Identifier: REL-2026-OCT-PICO-01\n"
    "Data Classification Policy: PUBLIC_SYNTHETIC\n\n"
    "Section 1: Telemetry & Ingestion Subsystem\n"
    "The telemetry ingestion gateway receives metric packets over local UDP socket 5400.\n"
    "metric_collection_enabled = true\n"
    "metric_reporting_interval = \"15s\"\n"
    "telemetry_buffer_capacity_mb = 128\n"
    "telemetry_endpoint_primary = \"http://127.0.0.1:9090/metrics\"\n"
    "telemetry_endpoint_secondary = \"http://127.0.0.1:9091/metrics\"\n"
    "dropped_packet_threshold_percent = 0.05\n\n"
    "Section 2: Database Replication Topology\n"
    "The storage cluster runs SQLite in WAL mode with secondary streaming replication.\n"
    "- Node db-replica-01: status=STANDBY_READ, priority=20, host=127.0.0.1:5433\n"
    "- Node db-replica-02: status=PRIMARY_WRITE, priority=100, host=127.0.0.1:5432\n"
    "- Node db-replica-03: status=STANDBY_READ, priority=10, host=127.0.0.1:5434\n"
    "All write transactions must commit to the PRIMARY_WRITE node with synchronous=NORMAL.\n"
    "max_sync_retry_count = 5\n"
    "replication_heartbeat_ms = 250\n\n"
    "Section 3: Security & Capability Allowlist\n"
    "Tool execution is restricted to allowlisted endpoints:\n"
    "allowlisted_tools = [\"read_file\", \"write_file\", \"query_telemetry\"]\n"
    "allow_arbitrary_shell = false\n"
    "human_authorization_threshold = \"SENSITIVE\"\n"
    "=== END SPECIFICATION ==="
)

BENCHMARK_PROMPTS: list[BenchmarkPrompt] = [
    # 1. Simple instruction following and concise responses
    BenchmarkPrompt(
        prompt_id="BENCH-INSTRUCT-01",
        category="instruction_following",
        prompt=(
            "Respond strictly following this instruction: List the three primary colors of light "
            "(additive primaries) in alphabetical order as a comma-separated list. "
            "Output only the colors and nothing else."
        ),
        expected_output_hint="blue, green, red",
        max_tokens=24,
    ),
    # 2. Summarization of a short, synthetic passage
    BenchmarkPrompt(
        prompt_id="BENCH-SUMM-01",
        category="summarization",
        prompt=(
            "Summarize the following project update in exactly two sentences:\n\n"
            "The infrastructure migration completed ahead of schedule on Tuesday evening. "
            "All background database connections were transitioned to write-ahead logging mode "
            "with zero data loss reported during the cutover. Secondary indexing and foreign "
            "key verification completed without errors. The engineering team has scheduled "
            "performance monitoring for the remainder of the release cycle."
        ),
        expected_output_hint="Two-sentence summary of successful infrastructure migration and WAL cutover.",
        max_tokens=96,
    ),
    # 3. Structured JSON output against an explicit schema
    BenchmarkPrompt(
        prompt_id="BENCH-JSON-01",
        category="structured_json",
        prompt=(
            "Output a valid JSON object matching this exact schema:\n"
            "{\n"
            '  "task_id": "task_101",\n'
            '  "service": "database",\n'
            '  "action": "VACUUM",\n'
            '  "priority": 2,\n'
            '  "requires_approval": false\n'
            "}\n"
            "Output ONLY the raw JSON object. Do not enclose it in markdown code fences or backticks, "
            "and do not output any explanatory text before or after the JSON."
        ),
        expected_output_hint='JSON object with keys task_id, service, action, priority, requires_approval.',
        max_tokens=80,
    ),
    # 4. Lightweight reasoning and multi-step instruction
    BenchmarkPrompt(
        prompt_id="BENCH-REASON-01",
        category="bounded_reasoning",
        prompt=(
            "Solve this concise logic problem step by step in under 50 words:\n"
            "Server A has 4 CPU cores. Server B has twice as many cores as Server A. "
            "Server C has 3 fewer cores than Server B. How many cores does Server C have?"
        ),
        expected_output_hint="Server C has 5 cores (A=4, B=8, C=8-3=5).",
        max_tokens=80,
    ),
    # 5. Context-heavy input within supported local context budget
    BenchmarkPrompt(
        prompt_id="BENCH-CONTEXT-01",
        category="context_heavy",
        prompt=(
            f"{SYNTHETIC_CONTEXT_DOCUMENT}\n\n"
            "Question: Based strictly on the specification document above, answer both parts concisely:\n"
            "1. What is the exact string value configured for 'metric_reporting_interval'?\n"
            "2. Which database replica node has the 'PRIMARY_WRITE' role?"
        ),
        expected_output_hint="metric_reporting_interval is '15s'; replica is 'db-replica-02'.",
        max_tokens=64,
    ),
    # 6. Coding / command interpretation (without execution)
    BenchmarkPrompt(
        prompt_id="BENCH-CODE-01",
        category="code_interpretation",
        prompt=(
            "Explain what the following PowerShell pipeline does without executing it:\n\n"
            "Get-ChildItem -Path C:\\logs -Filter *.log | "
            "Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } | "
            "Remove-Item -WhatIf\n\n"
            "State its purpose and explain the safety effect of the '-WhatIf' parameter in at most two sentences."
        ),
        expected_output_hint="Finds log files older than 30 days; -WhatIf simulates without actually deleting.",
        max_tokens=96,
    ),
    # 7. Classification or routing decision
    BenchmarkPrompt(
        prompt_id="BENCH-CLASSIFY-01",
        category="intent_classification",
        prompt=(
            "Classify the intent of this user message into one of: "
            "[SEARCH, CREATE, UPDATE, DELETE, UNKNOWN]. Output only the category name:\n\n"
            "'Can you remove all temporary task files created yesterday?'"
        ),
        expected_output_hint="DELETE",
        max_tokens=16,
    ),
    # 8. Entity extraction
    BenchmarkPrompt(
        prompt_id="BENCH-EXTRACT-01",
        category="entity_extraction",
        prompt=(
            "Extract the component names and their status from this log entry. "
            "Format your answer as 'Component: Status' on separate lines:\n\n"
            "Log 2026-10-18T00:00:00Z: [Gateway] ONLINE, [Database] ACTIVE, [Cache] DEGRADED, [Storage] ONLINE."
        ),
        expected_output_hint="List of components (Gateway, Database, Cache, Storage) with statuses.",
        max_tokens=64,
    ),
]


def evaluate_response_quality(prompt_id: str, response_text: str) -> tuple[float, bool, str]:
    """Evaluate response quality against explicit, task-specific criteria.

    Returns:
        (score [0.0 - 1.0], passed [True/False], details)
    """
    cleaned = response_text.strip()
    if not cleaned:
        return 0.0, False, "Response was empty."

    if prompt_id == "WARMUP-01":
        passed = "READY" in cleaned.upper()
        return (1.0 if passed else 0.0), passed, "Matches READY" if passed else "Missing READY"

    if prompt_id == "BENCH-INSTRUCT-01":
        lower = cleaned.lower()
        has_blue = "blue" in lower
        has_green = "green" in lower
        has_red = "red" in lower
        matches = sum([has_blue, has_green, has_red])
        passed = matches == 3
        score = round(matches / 3.0, 2)
        return score, passed, f"Found {matches}/3 target colors (blue, green, red)"

    if prompt_id == "BENCH-SUMM-01":
        lower = cleaned.lower()
        has_migration = "migration" in lower or "infrastructure" in lower
        has_db = "database" in lower or "wal" in lower or "logging" in lower
        passed = has_migration and has_db and len(cleaned) > 20
        score = 1.0 if passed else (0.5 if (has_migration or has_db) else 0.0)
        return score, passed, "Captured key infrastructure & database summary concepts" if passed else "Missing key summary concepts"

    if prompt_id == "BENCH-JSON-01":
        # Extract potential JSON substring if enclosed in fences
        candidate = cleaned
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            candidate = match.group(0)
        try:
            parsed = json.loads(candidate)
            if not isinstance(parsed, dict):
                return 0.0, False, "Parsed JSON is not an object."
            required_keys = {"task_id", "service", "action", "priority", "requires_approval"}
            missing = required_keys - set(parsed.keys())
            if not missing:
                return 1.0, True, "Valid JSON matching all required schema fields."
            score = round((len(required_keys) - len(missing)) / len(required_keys), 2)
            return score, False, f"JSON parsed but missing keys: {missing}"
        except Exception as exc:
            return 0.0, False, f"JSON parse error: {exc}"

    if prompt_id == "BENCH-REASON-01":
        lower = cleaned.lower()
        # Server C has 5 cores
        has_5 = "5" in cleaned or "five" in lower
        passed = has_5
        return (1.0 if passed else 0.0), passed, "Calculated 5 cores correctly" if passed else "Did not arrive at 5 cores"

    if prompt_id == "BENCH-CONTEXT-01":
        lower = cleaned.lower()
        has_interval = "15s" in lower or "15" in lower
        has_node = "db-replica-02" in lower or "replica-02" in lower
        passed = has_interval and has_node
        score = 1.0 if passed else (0.5 if (has_interval or has_node) else 0.0)
        return score, passed, "Extracted 15s and db-replica-02" if passed else f"Missing fields (interval: {has_interval}, node: {has_node})"

    if prompt_id == "BENCH-CODE-01":
        lower = cleaned.lower()
        mentions_logs = "log" in lower or "file" in lower
        mentions_days = "30" in lower or "old" in lower or "month" in lower
        mentions_whatif = "whatif" in lower or "simulate" in lower or "dry" in lower or "test" in lower or "prevent" in lower or "safety" in lower
        score = round((sum([mentions_logs, mentions_days, mentions_whatif])) / 3.0, 2)
        passed = mentions_whatif and (mentions_logs or mentions_days)
        return score, passed, f"Mentions log files ({mentions_logs}), age ({mentions_days}), WhatIf safety ({mentions_whatif})"

    if prompt_id == "BENCH-CLASSIFY-01":
        passed = "DELETE" in cleaned.upper()
        return (1.0 if passed else 0.0), passed, "Correctly classified DELETE" if passed else f"Classification was not DELETE ({cleaned[:30]})"

    if prompt_id == "BENCH-EXTRACT-01":
        lower = cleaned.lower()
        comps = ["gateway", "database", "cache", "storage"]
        found = [c for c in comps if c in lower]
        score = round(len(found) / 4.0, 2)
        passed = len(found) >= 3
        return score, passed, f"Extracted {len(found)}/4 components: {found}"

    return 1.0, True, "Completed without specific heuristic rule."
