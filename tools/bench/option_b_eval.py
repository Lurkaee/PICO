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
import io
import json
import logging
import math
import os
import re
import struct
import unicodedata
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, NamedTuple

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
VALID_MANIFEST_STATUSES: frozenset[str] = frozenset({
    "PENDING_RECORDINGS",
    "RECORDED",
    "EVALUATED",
})
QUALIFYING_MANIFEST_STATUSES: frozenset[str] = frozenset({
    "RECORDED",
    "EVALUATED",
})
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

KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS: frozenset[str] = frozenset({
    # Synthetic SAPI smoke fixtures
    "9e54a7c18ea693a189f3bbceeb5e282bfba9d32d3999e1eeafef6b8c94e09cb6",  # audio_cmd_short_1s.wav
    "c7921a9767f40775a6396b27e80f4955c3c0a520ca4f5d60ee458760fa50787a",  # audio_cmd_query_3s.wav
    "df600bb94ae27d530ee21fb1549488bb4a2e5d959549f57ebbf25287f9850c99",  # audio_cmd_workflow_5s.wav
    "ad2ba59c8fa7270e5b7c02b11340fa0fae534f3a2c4e64f7c2250269ff0caaa2",  # audio_cmd_dictation_10s.wav
    "12981559981be1ff71e626cfb74549f315a6b0c2a7c473c914ebfdce2f8db5a5",  # audio_cmd_negative_noise_1s.wav
    "876615ae90e4f8dcfc918c541c4efdf979aa7dc02bf17088b200b3f8e582846f",  # audio_cmd_silence_1s.wav
    # Historical prototype fixtures
    "59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e",  # jfk.wav
    "0e5e19033b3508fb604f1d1f8192ada2d8da26aec726965171937a4038a3b747",  # jfk_short_1s.wav
    "484d817df898649ca2a02b571969549723b170e1ce7237f83e464a6d49a1778a",  # jfk_query_3s.wav
    "8487d4aad0452b2a2ee34e61220ebbf7989faa3071a982ceded916cdb2058101",  # jfk_workflow_5s.wav
    "cd3d359bf2c9c803cd9af596d5c458bfaa58ebd088c035cc1c51a02de9b05d56",  # jfk_dictation_10s.wav
    "dcfea5712c43a43ba7ae8083afb39d36993e5a69c46e88b68aaa72b65cb615bb",  # vosk_digits.wav
})


class ValidatedAudioClip(NamedTuple):
    path: Path
    audio_bytes: bytes

_DIGIT_MAP = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _is_strict_int(val: Any) -> bool:
    """Return True if val is an int and strictly not a boolean."""
    return isinstance(val, int) and not isinstance(val, bool)


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
    manifest_display_name: str | None = None
    manifest_sha256: str | None = None
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
        historical_samples_count: int | None = None,
        historical_words_count: int | None = None,
        historical_edits_count: int | None = None,
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
            if (
                historical_samples_count is not None
                and historical_words_count is not None
                and historical_edits_count is not None
            ):
                detail = f"{historical_samples_count} historical real-speech fixtures, {historical_words_count} words, {historical_edits_count} edits"
            else:
                detail = "5 JFK slices + 1 Vosk digit sequence, 75 words, 2 edits"
            parts.append(
                f"Preliminary historical mixed real-speech WER={pct} (Macro WER={macro_w}, CER={macro_c}; "
                f"{detail}; informational prototype fixture)."
            )

        if smoke_wer is not None:
            parts.append(f"Synthetic SAPI smoke WER={round(smoke_wer * 100, 2)}% (CI smoke test only).")

        return " ".join(parts)


def normalize_text_for_option_b(text: str) -> str:
    """Normalize text for WER calculation (casefold, expand digits, remove punctuation, collapse whitespace)."""
    text = text.lower()

    def _replace_digit(m: re.Match[str]) -> str:
        ch = m.group(0)
        try:
            val = unicodedata.digit(ch)
            return f" {_DIGIT_MAP[str(val)]} "
        except (ValueError, KeyError):
            return f" {ch} "

    text = re.sub(r"\d", _replace_digit, text)
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


def validate_wav_riff_pcm_header(raw_bytes: bytes, filename: str) -> tuple[int, int, int, int]:
    """Parse and validate RIFF/WAVE header and PCM format down to binary chunk structure.

    Enforces:
    - RIFF container with WAVE format tag.
    - fmt chunk: uncompressed PCM (1), mono (1), 16000 Hz, 32000 byte rate, 2 block align, strictly 16 bits per sample.
    - data chunk: non-empty, frame-aligned payload matching declared chunk length.

    Returns:
        (n_channels, sampwidth, framerate, n_frames)

    Raises:
        OptionBAudioValidationError: If any byte-level constraint is violated.
    """
    total_len = len(raw_bytes)
    if total_len < 44:
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' is too short ({total_len} bytes) to contain a valid RIFF/WAVE header."
        )

    if raw_bytes[0:4] != b"RIFF":
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' missing RIFF signature."
        )

    riff_size = struct.unpack("<I", raw_bytes[4:8])[0]
    riff_end = 8 + riff_size
    if riff_end > total_len:
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' RIFF chunk size {riff_size} exceeds actual file length {total_len}."
        )
    if total_len > riff_end:
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' contains {total_len - riff_end} unauthorized trailing bytes beyond RIFF boundary."
        )

    if raw_bytes[8:12] != b"WAVE":
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' missing WAVE format tag."
        )

    offset = 12
    fmt_parsed = False
    data_parsed = False
    n_channels = 0
    framerate = 0
    bits_per_sample = 0
    n_frames = 0

    while offset + 8 <= riff_end:
        chunk_id = raw_bytes[offset:offset+4]
        chunk_size = struct.unpack("<I", raw_bytes[offset+4:offset+8])[0]
        chunk_data_start = offset + 8
        chunk_data_end = chunk_data_start + chunk_size
        pad = chunk_size % 2

        if chunk_data_end + pad > riff_end:
            raise OptionBAudioValidationError(
                f"Audio file '{filename}' chunk {chunk_id!r} declared size {chunk_size} exceeds RIFF boundary."
            )

        if chunk_id == b"fmt ":
            if fmt_parsed:
                raise OptionBAudioValidationError(
                    f"Audio file '{filename}' contains duplicate 'fmt ' chunk."
                )
            if chunk_size < 16:
                raise OptionBAudioValidationError(
                    f"Audio file '{filename}' fmt chunk size {chunk_size} is less than required 16 bytes."
                )
            wFormatTag, nChannels, nSamplesPerSec, nAvgBytesPerSec, nBlockAlign, wBitsPerSample = struct.unpack(
                "<HHIIHH", raw_bytes[chunk_data_start:chunk_data_start+16]
            )
            if wFormatTag != 1:  # WAVE_FORMAT_PCM
                raise OptionBAudioValidationError(
                    f"Audio compression violation for '{filename}': expected uncompressed PCM (format tag 1), got {wFormatTag}."
                )
            if nChannels != 1:
                raise OptionBAudioValidationError(
                    f"Audio channel format violation for '{filename}': expected mono (1 channel), got {nChannels}."
                )
            if nSamplesPerSec != 16000:
                raise OptionBAudioValidationError(
                    f"Audio sample rate violation for '{filename}': expected 16000 Hz, got {nSamplesPerSec} Hz."
                )
            if wBitsPerSample != 16:
                raise OptionBAudioValidationError(
                    f"Audio bit depth violation for '{filename}': expected strictly 16-bit PCM, got {wBitsPerSample}-bit."
                )
            if nBlockAlign != 2:
                raise OptionBAudioValidationError(
                    f"Audio block alignment violation for '{filename}': expected 2 bytes, got {nBlockAlign}."
                )
            if nAvgBytesPerSec != 32000:
                raise OptionBAudioValidationError(
                    f"Audio byte rate violation for '{filename}': expected 32000 bytes/sec, got {nAvgBytesPerSec}."
                )
            fmt_parsed = True
            n_channels = nChannels
            framerate = nSamplesPerSec
            bits_per_sample = wBitsPerSample
        elif chunk_id == b"data":
            if data_parsed:
                raise OptionBAudioValidationError(
                    f"Audio file '{filename}' contains duplicate 'data' chunk."
                )
            if not fmt_parsed:
                raise OptionBAudioValidationError(
                    f"Audio file '{filename}' contains data chunk before fmt chunk."
                )
            if chunk_size <= 0:
                raise OptionBAudioValidationError(
                    f"Audio payload violation for '{filename}': data chunk is empty (0 bytes)."
                )
            if chunk_size % 2 != 0:
                raise OptionBAudioValidationError(
                    f"Audio payload violation for '{filename}': data chunk size {chunk_size} not aligned to 16-bit samples."
                )
            n_frames = chunk_size // 2
            data_parsed = True

        offset = chunk_data_end + pad

    if offset != riff_end:
        raise OptionBAudioValidationError(
            f"Audio file '{filename}' has unexpected trailing or incomplete chunk data within RIFF container."
        )

    if not fmt_parsed:
        raise OptionBAudioValidationError(f"Audio file '{filename}' missing required fmt chunk.")
    if not data_parsed:
        raise OptionBAudioValidationError(f"Audio file '{filename}' missing required data chunk.")
    if n_frames <= 0:
        raise OptionBAudioValidationError(f"Audio payload violation for '{filename}': zero frames found.")

    return n_channels, 2, framerate, n_frames


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

    status = corpus_section.get("status")
    if not isinstance(status, str) or status not in VALID_MANIFEST_STATUSES:
        raise OptionBManifestError(
            f"Manifest 'formal_accuracy_corpus.status' must be one of {sorted(VALID_MANIFEST_STATUSES)}, got {status!r}."
        )

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
        if not _is_strict_int(wc) or wc != expected_meta["word_count"]:
            raise OptionBManifestError(
                f"Word count mismatch for '{cid}'. Expected {expected_meta['word_count']}, got {wc!r}."
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
    if not _is_strict_int(manifest_total_words) or manifest_total_words != CANONICAL_TOTAL_REFERENCE_WORDS:
        raise OptionBManifestError(
            f"Manifest 'total_reference_words' is {manifest_total_words!r}, expected {CANONICAL_TOTAL_REFERENCE_WORDS}."
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

        cid = clip_raw.get("command_id")
        if not isinstance(cid, str):
            raise OptionBManifestError(f"Clip at index {idx} has invalid non-string command_id: {cid!r}.")

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
        p = Path(fn)
        if (
            p.is_absolute()
            or ".." in p.parts
            or fn.startswith("/")
            or fn.startswith("\\")
            or bool(re.match(r"^[a-zA-Z]:", fn))
        ):
            raise OptionBManifestError(
                f"Clip '{cid}' filename '{fn}' must be a safe relative filename without directory traversal."
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
        if not _is_strict_int(size_bytes) or size_bytes <= 0:
            raise OptionBManifestError(f"Clip '{fn}' size_bytes must be a positive integer, got {size_bytes!r}.")

        # Audio parameters
        sr = clip_raw["sample_rate_hz"]
        if not _is_strict_int(sr) or sr != 16000:
            raise OptionBManifestError(f"Clip '{fn}' sample_rate_hz must be strictly 16000, got {sr!r}.")

        ch = clip_raw["channels"]
        if not _is_strict_int(ch) or ch != 1:
            raise OptionBManifestError(f"Clip '{fn}' channels must be strictly 1 (mono), got {ch!r}.")

        fmt = clip_raw["sample_format"]
        if fmt != "16-bit signed PCM (little-endian)":
            raise OptionBManifestError(
                f"Clip '{fn}' sample_format must be strictly '16-bit signed PCM (little-endian)', got '{fmt}'."
            )

        dur = clip_raw["duration_seconds"]
        if isinstance(dur, bool) or not isinstance(dur, (int, float)) or not math.isfinite(dur) or dur <= 0.0:
            raise OptionBManifestError(
                f"Clip '{fn}' duration_seconds must be a finite, positive number, got {dur!r}."
            )

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
) -> ValidatedAudioClip:
    """Validate local Option B audio file format, safety, size, and cryptographic digest.

    Returns:
        ValidatedAudioClip containing validated absolute Path and verified audio bytes.

    Raises:
        OptionBAudioValidationError: If file fails path, format, or cryptographic checks.
    """
    raw_candidate = corpus_dir / clip.filename
    if raw_candidate.is_symlink():
        raise OptionBAudioValidationError(
            f"Symlink rejected: '{clip.filename}' is a symbolic link; qualification audio clips must be regular files."
        )

    try:
        resolved_corpus = corpus_dir.resolve()
    except (OSError, RuntimeError) as exc:
        raise OptionBAudioValidationError(
            f"Failed to resolve corpus root: {getattr(exc, 'strerror', None) or exc}"
        ) from exc

    try:
        target_path = raw_candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise OptionBAudioValidationError(
            f"Failed to resolve audio file path for '{clip.filename}': {getattr(exc, 'strerror', None) or exc}"
        ) from exc

    # Path traversal & containment check
    try:
        target_path.relative_to(resolved_corpus)
    except ValueError:
        raise OptionBAudioValidationError(
            f"Path traversal detected: '{clip.filename}' resolves outside corpus root."
        )

    if not target_path.exists():
        raise OptionBAudioValidationError(
            f"Option B audio file not found: '{clip.filename}'."
        )

    if not target_path.is_file() or target_path.is_symlink():
        raise OptionBAudioValidationError(
            f"Option B audio path is not a regular file: '{clip.filename}'."
        )

    # Read bytes once to prevent TOCTOU and redundant disk reads
    try:
        raw_bytes = target_path.read_bytes()
    except OSError as exc:
        raise OptionBAudioValidationError(
            f"Failed to read audio file '{clip.filename}': {exc.strerror or exc}"
        ) from exc

    # Size check
    actual_size = len(raw_bytes)
    if actual_size != clip.size_bytes:
        raise OptionBAudioValidationError(
            f"File size mismatch for '{clip.filename}': actual {actual_size} bytes != manifest {clip.size_bytes} bytes."
        )

    # Cryptographic SHA-256 digest check
    actual_sha = hashlib.sha256(raw_bytes).hexdigest().lower()
    if actual_sha != clip.sha256_digest:
        raise OptionBAudioValidationError(
            f"SHA-256 digest mismatch for '{clip.filename}': actual '{actual_sha}' != manifest '{clip.sha256_digest}'."
        )

    # Reject known synthetic smoke and historical prototype fixtures
    if actual_sha in KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS:
        raise OptionBAudioValidationError(
            f"Security rejection: Clip '{clip.filename}' matches known synthetic smoke or historical prototype digest '{actual_sha}'. "
            "GATE-VOICE-02 strictly requires genuine, naturally spoken human recordings."
        )

    # 1. Byte-level RIFF/WAVE header and 16-bit PCM format validation
    riff_channels, riff_sampwidth, riff_framerate, riff_n_frames = validate_wav_riff_pcm_header(
        raw_bytes, clip.filename
    )

    # 2. Standard library wave parser validation
    try:
        with wave.open(io.BytesIO(raw_bytes), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            comptype = wf.getcomptype()
            n_frames = wf.getnframes()
            frame_bytes = wf.readframes(n_frames)
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

    expected_payload_bytes = n_frames * n_channels * sampwidth
    if len(frame_bytes) != expected_payload_bytes:
        raise OptionBAudioValidationError(
            f"PCM payload incomplete for '{clip.filename}': header declared {n_frames} frames ({expected_payload_bytes} bytes), "
            f"but payload contains {len(frame_bytes)} bytes."
        )

    actual_duration = n_frames / framerate
    if abs(clip.duration_seconds - actual_duration) > 0.1:
        raise OptionBAudioValidationError(
            f"Duration mismatch for '{clip.filename}': manifest declared {clip.duration_seconds}s, "
            f"audio stream contains {actual_duration:.3f}s."
        )

    return ValidatedAudioClip(path=target_path, audio_bytes=raw_bytes)


def evaluate_option_b_gate_02(
    engine: Any | None,
    manifest_path: Path,
    corpus_dir: Path | None,
    n_threads: int = 4,
    model_meta: dict[str, Any] | None = None,
    telemetry_poll_fn: Callable[[], None] | None = None,
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

    manifest_display = manifest_path.name
    manifest_raw: bytes | None = None
    try:
        manifest_raw = manifest_path.read_bytes()
        manifest_sha = hashlib.sha256(manifest_raw).hexdigest().lower()
    except Exception:
        manifest_sha = None

    result = OptionBGate02Result(
        model_identifier=model_id,
        model_digest=model_sha,
        manifest_display_name=manifest_display,
        manifest_sha256=manifest_sha,
    )

    # 1. Manifest file existence & load
    if not manifest_path.exists():
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Voice corpus manifest '{manifest_display}' not found"
        return result
    try:
        if manifest_raw is None:
            raise OSError("manifest bytes were not read")
        manifest_data = json.loads(manifest_raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Failed to parse manifest JSON for '{manifest_display}': JSONDecodeError ({exc.msg} at line {exc.lineno})"
        return result
    except OSError as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        err_msg = exc.strerror or type(exc).__name__
        result.reason = f"Failed to read manifest file '{manifest_display}': {type(exc).__name__} ({err_msg})"
        return result
    except Exception as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Failed to parse manifest JSON for '{manifest_display}': {type(exc).__name__}"
        return result
    # 2. Manifest structural validation FIRST before extracting status
    try:
        canonical_commands, clips = validate_option_b_manifest(manifest_data)
    except OptionBManifestError as exc:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Option B manifest validation error: {exc}"
        result.errors.append(str(exc))
        return result

    corpus_section = manifest_data.get("formal_accuracy_corpus", {})
    if isinstance(corpus_section, dict):
        result.manifest_status = corpus_section.get("status", "UNKNOWN")
    else:
        result.manifest_status = "UNKNOWN"

    # If clips array is empty, qualification is PENDING_RECORDINGS
    if not clips:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = (
            "Option B human command corpus has not been recorded; clips array is empty "
            f"(status: {result.manifest_status})"
        )
        return result

    # Prevent a non-qualifying manifest with clips from producing a formal measured result
    if result.manifest_status not in QUALIFYING_MANIFEST_STATUSES:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = (
            f"Option B manifest status '{result.manifest_status}' is not qualifying; "
            f"manifest status must be one of {sorted(QUALIFYING_MANIFEST_STATUSES)} to qualify."
        )
        return result

    # 3. Corpus directory validation
    if corpus_dir is None:
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = "Option B corpus directory argument (--option-b-corpus-dir) not provided"
        return result

    if not corpus_dir.exists() or not corpus_dir.is_dir():
        result.status = OptionBGateStatus.NOT_RUN.value
        result.reason = f"Option B corpus directory '{corpus_dir.name}' does not exist or is not a directory"
        return result

    # 4. Audio file safety, cryptographic validation, and physical alias detection
    validated_files: dict[str, ValidatedAudioClip] = {}
    seen_audio_digests: set[str] = set()
    seen_resolved_paths: set[Path] = set()
    seen_file_identities: set[tuple[int, int]] = set()

    for clip in clips:
        if clip.sha256_digest in seen_audio_digests:
            result.status = OptionBGateStatus.NOT_RUN.value
            result.reason = (
                f"Duplicate audio content detected: clip '{clip.filename}' ({clip.command_id}) "
                f"has identical SHA-256 digest to another clip in the corpus"
            )
            result.errors.append(result.reason)
            return result
        seen_audio_digests.add(clip.sha256_digest)

        candidate_path = corpus_dir / clip.filename
        if candidate_path.is_symlink():
            result.status = OptionBGateStatus.NOT_RUN.value
            result.reason = (
                f"Symlink rejected: clip '{clip.filename}' ({clip.command_id}) is a symbolic link; "
                "qualification audio clips must be regular files"
            )
            result.errors.append(result.reason)
            return result

        try:
            val_clip = validate_option_b_audio_file(clip, corpus_dir)
            real_path = val_clip.path.resolve()
            if real_path in seen_resolved_paths:
                result.status = OptionBGateStatus.NOT_RUN.value
                result.reason = (
                    f"Duplicate file alias detected: clip '{clip.filename}' ({clip.command_id}) "
                    f"resolves to already registered file"
                )
                result.errors.append(result.reason)
                return result
            seen_resolved_paths.add(real_path)

            st = val_clip.path.stat()
            file_ident = (st.st_dev, st.st_ino)
            if st.st_ino != 0 and file_ident in seen_file_identities:
                result.status = OptionBGateStatus.NOT_RUN.value
                result.reason = (
                    f"Duplicate physical file detected: clip '{clip.filename}' ({clip.command_id}) "
                    f"shares inode with another clip (hard link alias detected)"
                )
                result.errors.append(result.reason)
                return result
            if st.st_ino != 0:
                seen_file_identities.add(file_ident)

            validated_files[clip.command_id] = val_clip
        except (OptionBAudioValidationError, OSError, RuntimeError) as exc:
            result.status = OptionBGateStatus.NOT_RUN.value
            err_msg = getattr(exc, "strerror", None) or str(exc)
            result.reason = f"Audio validation failed for clip '{clip.filename}': {err_msg}"
            result.errors.append(err_msg)
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
        val_clip = validated_files[clip.command_id]
        cmd_meta = canonical_commands[clip.command_id]
        ref_text = cmd_meta.canonical_text

        if telemetry_poll_fn is not None:
            telemetry_poll_fn()
        try:
            engine_out = engine.transcribe_wav_bytes(val_clip.audio_bytes, n_threads=n_threads)
            if isinstance(engine_out, (tuple, list)):
                hyp_text = str(engine_out[0])
            elif isinstance(engine_out, str):
                hyp_text = engine_out
            else:
                hyp_text = str(engine_out)
        except Exception as exc:
            result.status = OptionBGateStatus.NOT_RUN.value
            result.reason = (
                f"Inference execution failed on Option B clip '{clip.filename}' ({clip.command_id}): {exc}"
            )
            result.errors.append(str(exc))
            return result
        finally:
            if telemetry_poll_fn is not None:
                telemetry_poll_fn()
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
