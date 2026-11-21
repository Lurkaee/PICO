"""PICO Option B STT Accuracy Qualification Evaluation (Milestone P11.3 Stage 2).

Provides deterministic loading, cryptographic validation, audio format verification,
and aggregate Word Error Rate (WER) evaluation for GATE-VOICE-02 against the formal
Option B human command corpus.

Invariants:
1. Strict 1:1 mapping between canonical_commands[].command_id and clips[].command_id.
2. Canonical denominator is strictly 49 reference words (2 + 8 + 13 + 26).
3. Human audio recordings remain local and are never committed to git or uploaded.
4. GATE-VOICE-02 threshold is strictly aggregate WER <= 12.0% (<= 5 edits across 49 words).
5. Missing, incomplete, unverified, or malformed inputs return NOT RUN, never inferred PASS.
6. Execution/runtime exceptions return NOT RUN, never misreported as accuracy FAIL.
7. Synthetic SAPI clips and historical JFK/Vosk prototype fixtures cannot satisfy GATE-VOICE-02.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("option_b_eval")

# Canonical 4-command reference specification (ADR 0005, Proposal 0006)
EXPECTED_CANONICAL_COMMANDS: dict[str, dict[str, Any]] = {
    "cmd_short_1s": {
        "canonical_text": "PICO stop.",
        "word_count": 2,
        "description": "Short imperative command",
    },
    "cmd_query_3s": {
        "canonical_text": "What tasks are currently waiting for my approval?",
        "word_count": 8,
        "description": "Query tasks command",
    },
    "cmd_workflow_5s": {
        "canonical_text": "Summarize the latest system logs and create a new draft task for review.",
        "word_count": 13,
        "description": "Workflow proposal command",
    },
    "cmd_dictation_10s": {
        "canonical_text": (
            "We need to verify the database migrations, ensure all foreign key constraints are enabled, "
            "and check that the memory service cleans up expired working memories automatically."
        ),
        "word_count": 26,
        "description": "Extended technical dictation",
    },
}

CANONICAL_TOTAL_REFERENCE_WORDS: int = 49
GATE_02_TARGET_WER_THRESHOLD_PCT: float = 12.0
REQUIRED_PROVENANCE_FIELDS: tuple[str, ...] = (
    "command_id",
    "filename",
    "sha256_digest",
    "size_bytes",
    "sample_rate_hz",
    "channels",
    "sample_format",
    "duration_seconds",
    "consenting_speaker_id",
    "physical_microphone_model",
    "acoustic_environment_description",
    "ground_truth_transcript",
)

_DIGIT_MAP = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


class OptionBCorpusError(Exception):
    """Base exception for Option B corpus validation and evaluation errors."""


class OptionBManifestError(OptionBCorpusError):
    """Raised when manifest structure, canonical commands, or clip schemas are invalid."""


class OptionBAudioValidationError(OptionBCorpusError):
    """Raised when audio files fail safety, format, or cryptographic checks."""


class OptionBGateStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT RUN"


@dataclass(frozen=True)
class CanonicalCommand:
    command_id: str
    canonical_text: str
    word_count: int
    description: str


@dataclass(frozen=True)
class OptionBClipManifest:
    command_id: str
    filename: str
    sha256_digest: str
    size_bytes: int
    sample_rate_hz: int
    channels: int
    sample_format: str
    duration_seconds: float
    consenting_speaker_id: str
    physical_microphone_model: str
    acoustic_environment_description: str
    ground_truth_transcript: str


@dataclass
class OptionBCommandResult:
    command_id: str
    filename: str
    reference_text: str
    reference_word_count: int
    hypothesis_text: str
    edit_distance: int
    substitutions: int
    deletions: int
    insertions: int
    wer_ratio: float


@dataclass
class OptionBGate02Result:
    gate_id: str = "GATE-VOICE-02"
    status: str = "NOT RUN"
    evaluation_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    target_wer_threshold_pct: float = GATE_02_TARGET_WER_THRESHOLD_PCT
    total_reference_words: int = CANONICAL_TOTAL_REFERENCE_WORDS
    aggregate_edit_count: int | None = None
    exact_reference_denominator: int = CANONICAL_TOTAL_REFERENCE_WORDS
    aggregate_wer_ratio: float | None = None
    aggregate_wer_pct: str | None = None
    command_results: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""
    manifest_status: str = "UNKNOWN"
    model_identifier: str | None = None
    model_digest: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def format_measured_summary(
        self,
        historical_mixed_wer: float | None = None,
        historical_macro_wer: float | None = None,
        historical_macro_cer: float | None = None,
        smoke_wer: float | None = None,
    ) -> str:
        """Format a clear, auditable Gate 02 summary for reports."""
        if self.status in ("PASS", "FAIL"):
            edits = self.aggregate_edit_count if self.aggregate_edit_count is not None else 0
            ratio_pct = self.aggregate_wer_pct or "N/A"
            return (
                f"Option B human corpus WER={ratio_pct} ({edits} edits / {self.exact_reference_denominator} reference words) "
                f"across 4 canonical commands. Target: <= {self.target_wer_threshold_pct}%. Status: {self.status}."
            )

        # NOT RUN case
        parts: list[str] = []
        if self.reason:
            parts.append(f"Option B evaluation {self.status}: {self.reason}.")
        else:
            parts.append(f"Option B evaluation {self.status}.")

        if historical_mixed_wer is not None:
            pct = f"{round(historical_mixed_wer * 100, 2)}%"
            macro_w = f"{round(historical_macro_wer * 100, 2)}%" if historical_macro_wer is not None else "N/A"
            macro_c = f"{round(historical_macro_cer * 100, 2)}%" if historical_macro_cer is not None else "N/A"
            parts.append(
                f"Preliminary historical mixed real-speech WER={pct} (Macro WER={macro_w}, CER={macro_c}; "
                "5 JFK slices + 1 Vosk digit sequence, 75 words, 2 edits; informational prototype fixture)."
            )

        if smoke_wer is not None:
            parts.append(f"Synthetic SAPI smoke WER={round(smoke_wer * 100, 2)}% (CI smoke test only).")

        return " ".join(parts)


def normalize_text_for_option_b(text: str) -> str:
    """Normalize text for WER calculation (casefold, expand digits, remove punctuation, collapse whitespace)."""
    text = text.lower()
    text = re.sub(r"\d", lambda m: f" {_DIGIT_MAP[m.group(0)]} ", text)
    text = re.sub(r"[^\w\s]", "", text)
    return " ".join(text.split())


def compute_levenshtein_ops(seq1: list[str] | str, seq2: list[str] | str) -> tuple[int, int, int, int]:
    """Compute Wagner-Fischer edit distance and operations between two sequences.

    Returns:
        (distance, substitutions, deletions, insertions)
    """
    m, n = len(seq1), len(seq2)
    dp = [[(0, 0, 0, 0)] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        dp[i][0] = (i, 0, i, 0)
    for j in range(1, n + 1):
        dp[0][j] = (j, 0, 0, j)

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if seq1[i - 1] == seq2[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                prev_sub = dp[i - 1][j - 1]
                prev_del = dp[i - 1][j]
                prev_ins = dp[i][j - 1]

                cand_sub = (prev_sub[0] + 1, prev_sub[1] + 1, prev_sub[2], prev_sub[3])
                cand_del = (prev_del[0] + 1, prev_del[1], prev_del[2] + 1, prev_del[3])
                cand_ins = (prev_ins[0] + 1, prev_ins[1], prev_ins[2], prev_ins[3] + 1)

                dp[i][j] = min(cand_sub, cand_del, cand_ins, key=lambda x: x[0])

    return dp[m][n]


def validate_option_b_manifest(
    manifest_data: dict[str, Any],
) -> tuple[dict[str, CanonicalCommand], list[OptionBClipManifest]]:
    """Validate Option B formal accuracy corpus structure and clips from manifest dictionary.

    Returns:
        tuple of (canonical_commands_dict, list_of_validated_clips)

    Raises:
        OptionBManifestError: If manifest metadata, canonical commands, or clip schemas are invalid.
    """
    if not isinstance(manifest_data, dict):
        raise OptionBManifestError("Manifest data must be a JSON object.")

    corpus_section = manifest_data.get("formal_accuracy_corpus")
    if not isinstance(corpus_section, dict):
        raise OptionBManifestError("Manifest missing 'formal_accuracy_corpus' object.")

    # 1. Validate canonical_commands
    raw_commands = corpus_section.get("canonical_commands")
    if not isinstance(raw_commands, list):
        raise OptionBManifestError("'canonical_commands' must be a list.")

    if len(raw_commands) != 4:
        raise OptionBManifestError(
            f"Expected exactly 4 canonical commands, found {len(raw_commands)}."
        )

    canonical_commands: dict[str, CanonicalCommand] = {}
    seen_command_ids: set[str] = set()
    total_words = 0

    for cmd_raw in raw_commands:
        if not isinstance(cmd_raw, dict):
            raise OptionBManifestError("Each canonical command entry must be a JSON object.")

        cid = cmd_raw.get("command_id")
        if not cid or not isinstance(cid, str):
            raise OptionBManifestError("Canonical command missing string 'command_id'.")

        if cid in seen_command_ids:
            raise OptionBManifestError(f"Duplicate canonical command_id: '{cid}'.")
        seen_command_ids.add(cid)

        if cid not in EXPECTED_CANONICAL_COMMANDS:
            raise OptionBManifestError(
                f"Unknown canonical command_id '{cid}'. Expected one of {list(EXPECTED_CANONICAL_COMMANDS.keys())}."
            )

        expected_meta = EXPECTED_CANONICAL_COMMANDS[cid]
        text = cmd_raw.get("canonical_text")
        if text != expected_meta["canonical_text"]:
            raise OptionBManifestError(
                f"Canonical text mismatch for '{cid}'. Expected '{expected_meta['canonical_text']}', got '{text}'."
            )

        wc = cmd_raw.get("word_count")
        if wc != expected_meta["word_count"]:
            raise OptionBManifestError(
                f"Word count mismatch for '{cid}'. Expected {expected_meta['word_count']}, got {wc}."
            )

        desc = cmd_raw.get("description", expected_meta["description"])
        cmd_obj = CanonicalCommand(
            command_id=cid,
            canonical_text=text,
            word_count=wc,
            description=desc,
        )
        canonical_commands[cid] = cmd_obj
        total_words += wc

    if total_words != CANONICAL_TOTAL_REFERENCE_WORDS:
        raise OptionBManifestError(
            f"Total canonical reference words sum to {total_words}, expected exactly {CANONICAL_TOTAL_REFERENCE_WORDS}."
        )

    manifest_total_words = corpus_section.get("total_reference_words")
    if manifest_total_words != CANONICAL_TOTAL_REFERENCE_WORDS:
        raise OptionBManifestError(
            f"Manifest 'total_reference_words' is {manifest_total_words}, expected {CANONICAL_TOTAL_REFERENCE_WORDS}."
        )

    # 2. Validate clips
    raw_clips = corpus_section.get("clips")
    if not isinstance(raw_clips, list):
        raise OptionBManifestError("'clips' must be a list.")

    # Empty clips array represents PENDING_RECORDINGS
    if len(raw_clips) == 0:
        return canonical_commands, []

    if len(raw_clips) != 4:
        raise OptionBManifestError(
            f"Expected exactly 4 human recordings in 'clips' (one per canonical command), found {len(raw_clips)}."
        )

    validated_clips: list[OptionBClipManifest] = []
    seen_clip_commands: set[str] = set()
    seen_filenames: set[str] = set()
    operator_id: str | None = None

    for idx, clip_raw in enumerate(raw_clips):
        if not isinstance(clip_raw, dict):
            raise OptionBManifestError(f"Clip at index {idx} must be a JSON object.")

        # Check all required provenance fields
        for req_field in REQUIRED_PROVENANCE_FIELDS:
            if req_field not in clip_raw:
                raise OptionBManifestError(
                    f"Clip at index {idx} missing required provenance field '{req_field}'."
                )

        cid = clip_raw["command_id"]
        if cid not in canonical_commands:
            raise OptionBManifestError(
                f"Clip '{clip_raw.get('filename')}' references unknown command_id '{cid}'."
            )

        if cid in seen_clip_commands:
            raise OptionBManifestError(
                f"Duplicate clip registration for canonical command '{cid}'."
            )
        seen_clip_commands.add(cid)

        fn = clip_raw["filename"]
        if not isinstance(fn, str) or not fn.strip():
            raise OptionBManifestError(f"Clip '{cid}' has invalid or empty filename.")

        # Enforce safe relative filename (no path traversal, no absolute paths)
        if os.path.isabs(fn) or ".." in fn or "/" in fn or "\\" in fn:
            raise OptionBManifestError(
                f"Clip '{cid}' filename '{fn}' must be a simple, safe relative filename without directory traversal."
            )

        if fn in seen_filenames:
            raise OptionBManifestError(f"Duplicate clip filename '{fn}' in manifest.")
        seen_filenames.add(fn)

        # Cryptographic digest format
        digest = clip_raw["sha256_digest"]
        if not isinstance(digest, str) or not re.match(r"^[a-f0-9]{64}$", digest.lower()):
            raise OptionBManifestError(
                f"Clip '{fn}' has invalid sha256_digest: '{digest}'. Must be 64-character lowercase hex string."
            )

        # Size check
        size_bytes = clip_raw["size_bytes"]
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise OptionBManifestError(f"Clip '{fn}' size_bytes must be a positive integer.")

        # Audio parameters
        sr = clip_raw["sample_rate_hz"]
        if sr != 16000:
            raise OptionBManifestError(f"Clip '{fn}' sample_rate_hz must be strictly 16000, got {sr}.")

        ch = clip_raw["channels"]
        if ch != 1:
            raise OptionBManifestError(f"Clip '{fn}' channels must be strictly 1 (mono), got {ch}.")

        fmt = clip_raw["sample_format"]
        if fmt != "16-bit signed PCM (little-endian)":
            raise OptionBManifestError(
                f"Clip '{fn}' sample_format must be strictly '16-bit signed PCM (little-endian)', got '{fmt}'."
            )

        dur = clip_raw["duration_seconds"]
        if not isinstance(dur, (int, float)) or dur <= 0.0:
            raise OptionBManifestError(f"Clip '{fn}' duration_seconds must be a positive number.")

        # Consenting operator provenance
        speaker_id = clip_raw["consenting_speaker_id"]
        if not isinstance(speaker_id, str) or not speaker_id.strip():
            raise OptionBManifestError(f"Clip '{fn}' missing non-empty 'consenting_speaker_id'.")

        if operator_id is None:
            operator_id = speaker_id.strip()
        elif operator_id != speaker_id.strip():
            raise OptionBManifestError(
                f"Corpus consistency violation: Clip '{fn}' consenting_speaker_id '{speaker_id}' does not match "
                f"corpus operator '{operator_id}'. All 4 clips must originate from one consistent consenting operator."
            )

        mic = clip_raw["physical_microphone_model"]
        if not isinstance(mic, str) or not mic.strip():
            raise OptionBManifestError(f"Clip '{fn}' missing non-empty 'physical_microphone_model'.")

        env = clip_raw["acoustic_environment_description"]
        if not isinstance(env, str) or not env.strip():
            raise OptionBManifestError(f"Clip '{fn}' missing non-empty 'acoustic_environment_description'.")

        gt_text = clip_raw["ground_truth_transcript"]
        expected_text = canonical_commands[cid].canonical_text
        if not isinstance(gt_text, str) or normalize_text_for_option_b(gt_text) != normalize_text_for_option_b(expected_text):
            raise OptionBManifestError(
                f"Clip '{fn}' ground_truth_transcript '{gt_text}' deviates from canonical text '{expected_text}'."
            )

        clip_obj = OptionBClipManifest(
            command_id=cid,
            filename=fn,
            sha256_digest=digest.lower(),
            size_bytes=size_bytes,
            sample_rate_hz=sr,
            channels=ch,
            sample_format=fmt,
            duration_seconds=float(dur),
            consenting_speaker_id=operator_id,
            physical_microphone_model=mic.strip(),
            acoustic_environment_description=env.strip(),
            ground_truth_transcript=gt_text.strip(),
        )
        validated_clips.append(clip_obj)

    # Ensure all 4 canonical commands are covered
    if seen_clip_commands != set(canonical_commands.keys()):
        missing = set(canonical_commands.keys()) - seen_clip_commands
        raise OptionBManifestError(
            f"Clips array does not cover all canonical commands. Missing: {missing}."
        )

    return canonical_commands, validated_clips


def validate_option_b_audio_file(
    clip: OptionBClipManifest,
    corpus_dir: Path,
) -> Path:
    """Validate local Option B audio file format, safety, size, and cryptographic digest.

    Returns:
        Validated absolute Path to the WAV file.

    Raises:
        OptionBAudioValidationError: If file fails path, format, or cryptographic checks.
    """
    resolved_corpus = corpus_dir.resolve()
    target_path = (corpus_dir / clip.filename).resolve()

    # Path traversal & containment check
    try:
        target_path.relative_to(resolved_corpus)
    except ValueError:
        raise OptionBAudioValidationError(
            f"Path traversal detected: '{clip.filename}' resolves outside corpus root '{resolved_corpus}'."
        )

    if not target_path.exists():
        raise OptionBAudioValidationError(
            f"Option B audio file not found: '{target_path}'."
        )

    if not target_path.is_file():
        raise OptionBAudioValidationError(
            f"Option B audio path is not a regular file: '{target_path}'."
        )

    # Size check
    actual_size = target_path.stat().st_size
    if actual_size != clip.size_bytes:
        raise OptionBAudioValidationError(
            f"File size mismatch for '{clip.filename}': actual {actual_size} bytes != manifest {clip.size_bytes} bytes."
        )

    # Cryptographic SHA-256 digest check
    hasher = hashlib.sha256()
    with target_path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    actual_sha = hasher.hexdigest().lower()

    if actual_sha != clip.sha256_digest:
        raise OptionBAudioValidationError(
            f"SHA-256 digest mismatch for '{clip.filename}': actual '{actual_sha}' != manifest '{clip.sha256_digest}'."
        )

    # RIFF/WAVE header and PCM format validation
    try:
        with wave.open(str(target_path), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            comptype = wf.getcomptype()
            n_frames = wf.getnframes()
    except Exception as exc:
        raise OptionBAudioValidationError(
            f"Failed to parse RIFF/WAVE header for '{clip.filename}': {exc}"
        ) from exc

    if n_channels != 1:
        raise OptionBAudioValidationError(
            f"Audio channel format violation for '{clip.filename}': expected mono (1 channel), got {n_channels}."
        )

    if sampwidth != 2:
        raise OptionBAudioValidationError(
            f"Audio bit depth violation for '{clip.filename}': expected 16-bit (sampwidth=2), got {sampwidth}."
        )

    if framerate != 16000:
        raise OptionBAudioValidationError(
            f"Audio sample rate violation for '{clip.filename}': expected 16000 Hz, got {framerate} Hz."
        )

    if comptype != "NONE":
        raise OptionBAudioValidationError(
            f"Audio compression violation for '{clip.filename}': expected uncompressed PCM ('NONE'), got '{comptype}'."
        )

    if n_frames <= 0:
        raise OptionBAudioValidationError(
            f"Audio payload violation for '{clip.filename}': zero frames found."
        )

    return target_path


def evaluate_option_b_gate_02(
    engine: Any | None,
    manifest_path: Path,
    corpus_dir: Path | None,
    n_threads: int = 4,
    model_meta: dict[str, Any] | None = None,
) -> OptionBGate02Result:
    """Evaluate formal GATE-VOICE-02 accuracy against the Option B human command corpus.

    Calculates exact aggregate WER across the 49 reference words:
        aggregate_WER = sum(edits) / 49

    Deterministic Gate Decisions:
        PASS: aggregate WER <= 12.0% (<= 5 edits across 49 words)
        FAIL: aggregate WER > 12.0% (>= 6 edits across 49 words)
        NOT RUN: missing, incomplete, unverified, or malformed inputs, or uninitialized runtime.
    """
    model_id = model_meta.get("filename") if model_meta else None
    model_sha = model_meta.get("sha256") if model_meta else None

    result = OptionBGate02Result(
        model_identifier=model_id,
        model_digest=model_sha,
    )

    # 1. Manifest file existence & load
    if not manifest_path.exists():
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Voice corpus manifest not found at '{manifest_path}'"
        return result

    try:
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
    except Exception as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Failed to parse manifest JSON at '{manifest_path}': {exc}"
        return result

    result.manifest_status = manifest_data.get("formal_accuracy_corpus", {}).get("status", "UNKNOWN")

    # 2. Manifest structural validation
    try:
        canonical_commands, clips = validate_option_b_manifest(manifest_data)
    except OptionBManifestError as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Option B manifest validation error: {exc}"
        result.errors.append(str(exc))
        return result

    # If clips array is empty, qualification is PENDING_RECORDINGS
    if not clips:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = (
            "Option B human command corpus has not been recorded; clips array is empty "
            f"(status: {result.manifest_status})"
        )
        return result

    # 3. Corpus directory validation
    if corpus_dir is None:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = "Option B corpus directory argument (--option-b-corpus-dir) not provided"
        return result

    if not corpus_dir.exists() or not corpus_dir.is_dir():
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Option B corpus directory '{corpus_dir}' does not exist or is not a directory"
        return result

    # 4. Audio file safety and cryptographic validation
    validated_files: dict[str, Path] = {}
    for clip in clips:
        try:
            file_path = validate_option_b_audio_file(clip, corpus_dir)
            validated_files[clip.command_id] = file_path
        except OptionBAudioValidationError as exc:
            result.status = OptionBGateStatus.NOT_RUN.value
            result.reason = f"Audio validation failed for clip '{clip.filename}': {exc}"
            result.errors.append(str(exc))
            return result

    # 5. Native inference engine availability
    if engine is None:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = "PicoWhisperEngine is not initialized or unavailable"
        return result

    # 6. Execute transcription and compute edit operations
    total_edits = 0
    cmd_results: list[dict[str, Any]] = []

    # Sort clips in canonical command order
    ordered_clips = sorted(
        clips,
        key=lambda c: list(EXPECTED_CANONICAL_COMMANDS.keys()).index(c.command_id),
    )

    for clip in ordered_clips:
        file_path = validated_files[clip.command_id]
        cmd_meta = canonical_commands[clip.command_id]
        ref_text = cmd_meta.canonical_text

        try:
            with file_path.open("rb") as f:
                wav_bytes = f.read()
            hyp_text = engine.transcribe_wav_bytes(wav_bytes, n_threads=n_threads)
        except Exception as exc:
            result.status = OptionBGateStatus.NOT_RUN.value
            result.reason = (
                f"Inference execution failed on Option B clip '{clip.filename}' ({clip.command_id}): {exc}"
            )
            result.errors.append(str(exc))
            return result

        norm_ref = normalize_text_for_option_b(ref_text)
        norm_hyp = normalize_text_for_option_b(hyp_text)

        ref_tokens = norm_ref.split()
        hyp_tokens = norm_hyp.split()

        dist, subs, dels, ins = compute_levenshtein_ops(ref_tokens, hyp_tokens)
        total_edits += dist
        cmd_wer = round(dist / len(ref_tokens), 4) if ref_tokens else 0.0

        cmd_res = OptionBCommandResult(
            command_id=clip.command_id,
            filename=clip.filename,
            reference_text=ref_text,
            reference_word_count=len(ref_tokens),
            hypothesis_text=hyp_text,
            edit_distance=dist,
            substitutions=subs,
            deletions=dels,
            insertions=ins,
            wer_ratio=cmd_wer,
        )
        cmd_results.append(asdict(cmd_res))

    result.command_results = cmd_results
    result.aggregate_edit_count = total_edits
    result.exact_reference_denominator = CANONICAL_TOTAL_REFERENCE_WORDS

    wer_ratio = total_edits / CANONICAL_TOTAL_REFERENCE_WORDS
    result.aggregate_wer_ratio = round(wer_ratio, 6)
    result.aggregate_wer_pct = f"{round(wer_ratio * 100, 2)}%"

    # Exact threshold decision: aggregate WER <= 12.0%
    # Using integer arithmetic to avoid display rounding distortion:
    # total_edits / 49 <= 12 / 100 <=> total_edits * 100 <= 12 * 49 (588)
    # 5 edits * 100 = 500 <= 588 -> PASS (5/49 = 10.204%)
    # 6 edits * 100 = 600 > 588 -> FAIL (6/49 = 12.245%)
    if (total_edits * 100) <= (GATE_02_TARGET_WER_THRESHOLD_PCT * CANONICAL_TOTAL_REFERENCE_WORDS):
        result.status = OptionBGateStatus.PASS.value
        result.reason = (
            f"Aggregate WER {result.aggregate_wer_pct} ({total_edits} edits / 49 words) "
            f"satisfies <= {GATE_02_TARGET_WER_THRESHOLD_PCT}% threshold."
        )
    else:
        result.status = OptionBGateStatus.FAIL.value
        result.reason = (
            f"Aggregate WER {result.aggregate_wer_pct} ({total_edits} edits / 49 words) "
            f"exceeds <= {GATE_02_TARGET_WER_THRESHOLD_PCT}% threshold."
        )

    return result
