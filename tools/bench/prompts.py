"""Synthetic, non-sensitive prompts for PICO local model benchmark runner."""

from __future__ import annotations

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

BENCHMARK_PROMPTS: list[BenchmarkPrompt] = [
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
]
