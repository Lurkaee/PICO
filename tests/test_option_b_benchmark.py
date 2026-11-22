"""Automated tests for Option B STT Accuracy Qualification Tooling (Milestone P11.3 Stage 2).

Verifies:
1. Manifest and corpus validation (canonical commands, clip schemas, 1:1 mapping, single consenting operator).
2. Audio format, safety, and cryptographic checks (16kHz mono 16-bit PCM, SHA-256 digest, size, path traversal).
3. Exact WER arithmetic and deterministic GATE-VOICE-02 decisions:
   - 0 errors / 49 words -> PASS (0.00%)
   - 5 errors / 49 words -> PASS (10.20% <= 12.0%)
   - 6 errors / 49 words -> FAIL (12.24% > 12.0%)
   - > 6 errors -> FAIL
   - Missing/unverified input -> NOT RUN
   - Runtime unavailable / inference exception -> NOT RUN (never misreported as FAIL)
   - Synthetic SAPI / historical JFK/Vosk fixtures cannot satisfy GATE-VOICE-02
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import struct
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.bench import voice_bench
from tools.bench.option_b_eval import (
    CANONICAL_TOTAL_REFERENCE_WORDS,
    EXPECTED_CANONICAL_COMMANDS,
    GATE_02_TARGET_WER_THRESHOLD_PCT,
    OptionBAudioValidationError,
    OptionBClipManifest,
    OptionBGate02Result,
    OptionBGateStatus,
    OptionBManifestError,
    compute_levenshtein_ops,
    evaluate_option_b_gate_02,
    normalize_text_for_option_b,
    validate_option_b_audio_file,
    validate_option_b_manifest,
)


def create_test_wav_bytes(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
    fill_byte: bytes = b"\x00\x00",
) -> bytes:
    """Helper to generate in-memory RIFF/WAVE audio byte arrays for testing."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        n_frames = max(1, int(duration_s * sample_rate))
        wf.writeframes(fill_byte * n_frames)
    return buf.getvalue()


def create_12bit_test_wav_bytes(
    duration_s: float = 1.0,
    sample_rate: int = 16000,
) -> bytes:
    """Helper to craft an in-memory RIFF/WAVE file declaring 12 bits per sample in fmt chunk."""
    n_frames = max(1, int(duration_s * sample_rate))
    pcm_data = b"\x00\x00" * n_frames
    data_size = len(pcm_data)
    # wFormatTag=1, nChannels=1, nSamplesPerSec=16000, nAvgBytesPerSec=32000, nBlockAlign=2, wBitsPerSample=12
    fmt_chunk = struct.pack("<4sIHHIIHH", b"fmt ", 16, 1, 1, sample_rate, sample_rate * 2, 2, 12)
    data_chunk = struct.pack("<4sI", b"data", data_size) + pcm_data
    riff_payload = b"WAVE" + fmt_chunk + data_chunk
    riff_header = struct.pack("<4sI", b"RIFF", len(riff_payload))
    return riff_header + riff_payload


def create_mock_manifest_dict(clips: list[dict] | None = None) -> dict:
    """Helper to generate a valid manifest dictionary with optional clips."""
    canonical_commands_list = []
    for cid, meta in EXPECTED_CANONICAL_COMMANDS.items():
        canonical_commands_list.append({
            "command_id": cid,
            "canonical_text": meta["canonical_text"],
            "word_count": meta["word_count"],
            "description": meta["description"],
        })

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "PICO Voice Evaluation Corpus Manifest",
        "formal_accuracy_corpus": {
            "status": "RECORDED" if clips else "PENDING_RECORDINGS",
            "specification": "Option B — Naturally spoken human recordings of the four canonical command texts",
            "required_format": "16 kHz, 16-bit mono signed PCM (little-endian) RIFF/WAVE",
            "target_wer_threshold": 12.0,
            "total_reference_words": 49,
            "provenance_requirements": [
                "command_id", "filename", "sha256_digest", "size_bytes",
                "sample_rate_hz", "channels", "sample_format", "duration_seconds",
                "consenting_speaker_id", "physical_microphone_model",
                "acoustic_environment_description", "ground_truth_transcript"
            ],
            "canonical_commands": canonical_commands_list,
            "clips": clips if clips is not None else [],
        },
        "smoke_test_clips": [],
        "real_speech_clips": [],
    }


def create_mock_clip_entry(
    command_id: str,
    filename: str,
    audio_bytes: bytes,
    speaker_id: str = "operator_alpha_01",
    ground_truth: str | None = None,
    duration_s: float | None = None,
) -> dict:
    """Helper to generate a valid clip manifest entry matching generated audio bytes."""
    gt = ground_truth or EXPECTED_CANONICAL_COMMANDS[command_id]["canonical_text"]
    sha = hashlib.sha256(audio_bytes).hexdigest().lower()
    if duration_s is None:
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
                dur = round(wf.getnframes() / wf.getframerate(), 3)
        except Exception:
            dur = 1.0
    else:
        dur = duration_s
    return {
        "command_id": command_id,
        "filename": filename,
        "sha256_digest": sha,
        "size_bytes": len(audio_bytes),
        "sample_rate_hz": 16000,
        "channels": 1,
        "sample_format": "16-bit signed PCM (little-endian)",
        "duration_seconds": dur,
        "consenting_speaker_id": speaker_id,
        "physical_microphone_model": "Shure SM7B via Focusrite Scarlett 2i2",
        "acoustic_environment_description": "Treated domestic office, background noise < 30 dBA",
        "ground_truth_transcript": gt,
    }


# ==============================================================================
# 1. Manifest and Corpus Validation Tests
# ==============================================================================

class TestOptionBManifestValidation:
    """Validates structural and provenance requirements for Option B manifest."""

    def test_valid_manifest_with_empty_clips_pending_recordings(self):
        manifest_data = create_mock_manifest_dict(clips=[])
        canonical_cmds, clips = validate_option_b_manifest(manifest_data)
        assert len(canonical_cmds) == 4
        assert len(clips) == 0
        assert sum(cmd.word_count for cmd in canonical_cmds.values()) == 49

    def test_valid_manifest_with_four_canonical_clips(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        canonical_cmds, clips = validate_option_b_manifest(manifest_data)
        assert len(canonical_cmds) == 4
        assert len(clips) == 4
        assert [c.command_id for c in clips] == [
            "cmd_short_1s", "cmd_query_3s", "cmd_workflow_5s", "cmd_dictation_10s"
        ]

    def test_missing_formal_accuracy_corpus_section(self):
        manifest_data = {"title": "PICO"}
        with pytest.raises(OptionBManifestError, match="missing 'formal_accuracy_corpus'"):
            validate_option_b_manifest(manifest_data)

    def test_incomplete_canonical_commands_less_than_four(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["canonical_commands"] = manifest_data["formal_accuracy_corpus"]["canonical_commands"][:3]
        with pytest.raises(OptionBManifestError, match="Expected exactly 4 canonical commands, found 3"):
            validate_option_b_manifest(manifest_data)

    def test_duplicate_canonical_command_id(self):
        manifest_data = create_mock_manifest_dict()
        cmds = manifest_data["formal_accuracy_corpus"]["canonical_commands"]
        cmds[1] = copy.deepcopy(cmds[0])
        with pytest.raises(OptionBManifestError, match="Duplicate canonical command_id"):
            validate_option_b_manifest(manifest_data)

    def test_unknown_canonical_command_id(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["canonical_commands"][0]["command_id"] = "cmd_unknown_99s"
        with pytest.raises(OptionBManifestError, match="Unknown canonical command_id"):
            validate_option_b_manifest(manifest_data)

    def test_canonical_text_mismatch(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["canonical_commands"][0]["canonical_text"] = "Wrong text here."
        with pytest.raises(OptionBManifestError, match="Canonical text mismatch"):
            validate_option_b_manifest(manifest_data)

    def test_canonical_word_count_mismatch(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["canonical_commands"][0]["word_count"] = 99
        with pytest.raises(OptionBManifestError, match="Word count mismatch"):
            validate_option_b_manifest(manifest_data)

    def test_total_reference_words_not_49(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["total_reference_words"] = 50
        with pytest.raises(OptionBManifestError, match="total_reference_words' is 50, expected 49"):
            validate_option_b_manifest(manifest_data)

    def test_incomplete_clips_three_of_four_present(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="Expected exactly 4 human recordings in 'clips'"):
            validate_option_b_manifest(manifest_data)

    def test_duplicate_clip_command_id(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s_a.wav", wav),
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s_b.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="Duplicate clip registration for canonical command"):
            validate_option_b_manifest(manifest_data)

    def test_duplicate_clip_filename(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "audio_same.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "audio_same.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="Duplicate clip filename 'audio_same.wav'"):
            validate_option_b_manifest(manifest_data)

    def test_unknown_clip_command_id(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        clips_raw[0]["command_id"] = "cmd_extra_command"
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="references unknown command_id"):
            validate_option_b_manifest(manifest_data)

    def test_missing_provenance_field(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        del clips_raw[0]["physical_microphone_model"]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="missing required provenance field 'physical_microphone_model'"):
            validate_option_b_manifest(manifest_data)

    def test_inconsistent_consenting_speaker_id_across_clips(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav, speaker_id="operator_alpha"),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav, speaker_id="operator_alpha"),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav, speaker_id="operator_alpha"),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav, speaker_id="operator_beta"),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="Corpus consistency violation.*operator_alpha"):
            validate_option_b_manifest(manifest_data)

    def test_mismatched_ground_truth_transcript(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav, ground_truth="PICO start now."),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="deviates from canonical text"):
            validate_option_b_manifest(manifest_data)

    def test_path_traversal_in_filename_rejected(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "../../secret.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="safe relative filename without directory traversal"):
            validate_option_b_manifest(manifest_data)

    def test_clip_non_string_command_id_rejected(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        clips_raw[0]["command_id"] = ["cmd_short_1s"]  # invalid non-string
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="invalid non-string command_id"):
            validate_option_b_manifest(manifest_data)

    def test_safe_nested_relative_filename_accepted(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "session1/cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "session1/cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "session1/cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "session1/cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        canonical_cmds, clips = validate_option_b_manifest(manifest_data)
        assert len(clips) == 4
        assert clips[0].filename == "session1/cmd_short_1s.wav"

    def test_duration_nan_rejected(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav, duration_s=float("nan")),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="duration_seconds must be a finite, positive number"):
            validate_option_b_manifest(manifest_data)

    def test_duration_infinity_rejected(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav, duration_s=float("inf")),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="duration_seconds must be a finite, positive number"):
            validate_option_b_manifest(manifest_data)

    def test_duration_negative_or_zero_rejected(self):
        wav = create_test_wav_bytes(1.0)
        for bad_dur in [0.0, -1.5]:
            clips_raw = [
                create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav, duration_s=bad_dur),
                create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
                create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
                create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
            ]
            manifest_data = create_mock_manifest_dict(clips=clips_raw)
            with pytest.raises(OptionBManifestError, match="duration_seconds must be a finite, positive number"):
                validate_option_b_manifest(manifest_data)

    def test_duration_boolean_rejected(self):
        wav = create_test_wav_bytes(1.0)
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        clips_raw[0]["duration_seconds"] = True  # bool subclassing trap
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="duration_seconds must be a finite, positive number"):
            validate_option_b_manifest(manifest_data)

    def test_integer_fields_reject_booleans(self):
        wav = create_test_wav_bytes(1.0)
        # Test size_bytes = True
        clips_raw = [
            create_mock_clip_entry("cmd_short_1s", "cmd_short_1s.wav", wav),
            create_mock_clip_entry("cmd_query_3s", "cmd_query_3s.wav", wav),
            create_mock_clip_entry("cmd_workflow_5s", "cmd_workflow_5s.wav", wav),
            create_mock_clip_entry("cmd_dictation_10s", "cmd_dictation_10s.wav", wav),
        ]
        clips_raw[0]["size_bytes"] = True
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="size_bytes must be a positive integer"):
            validate_option_b_manifest(manifest_data)

        # Test sample_rate_hz = True
        clips_raw[0]["size_bytes"] = len(wav)
        clips_raw[0]["sample_rate_hz"] = True
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="sample_rate_hz must be strictly 16000"):
            validate_option_b_manifest(manifest_data)

        # Test channels = True
        clips_raw[0]["sample_rate_hz"] = 16000
        clips_raw[0]["channels"] = True
        manifest_data = create_mock_manifest_dict(clips=clips_raw)
        with pytest.raises(OptionBManifestError, match="channels must be strictly 1"):
            validate_option_b_manifest(manifest_data)

    def test_canonical_word_count_and_total_reject_booleans(self):
        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["canonical_commands"][0]["word_count"] = True
        with pytest.raises(OptionBManifestError, match="Word count mismatch"):
            validate_option_b_manifest(manifest_data)

        manifest_data = create_mock_manifest_dict()
        manifest_data["formal_accuracy_corpus"]["total_reference_words"] = True
        with pytest.raises(OptionBManifestError, match="total_reference_words"):
            validate_option_b_manifest(manifest_data)

    def test_manifest_status_allowlist_validation(self):
        manifest_data = create_mock_manifest_dict()
        # Unknown status
        manifest_data["formal_accuracy_corpus"]["status"] = "INVALID_STATUS"
        with pytest.raises(OptionBManifestError, match="formal_accuracy_corpus.status.*must be one of"):
            validate_option_b_manifest(manifest_data)

        # Non-string status
        manifest_data["formal_accuracy_corpus"]["status"] = 123
        with pytest.raises(OptionBManifestError, match="formal_accuracy_corpus.status.*must be one of"):
            validate_option_b_manifest(manifest_data)

        # Missing status
        del manifest_data["formal_accuracy_corpus"]["status"]
        with pytest.raises(OptionBManifestError, match="formal_accuracy_corpus.status.*must be one of"):
            validate_option_b_manifest(manifest_data)

        # EVALUATED is valid
        manifest_data["formal_accuracy_corpus"]["status"] = "EVALUATED"
        canonical_cmds, _ = validate_option_b_manifest(manifest_data)
        assert len(canonical_cmds) == 4


class TestOptionBTextNormalization:
    """Validates normalization rules (casefolding, digit expansion, punctuation removal)."""

    def test_unicode_decimal_digit_normalization(self):
        norm = normalize_text_for_option_b("PICO stop １")
        assert norm == "pico stop one"
        norm2 = normalize_text_for_option_b("count 1 2 3")
        assert norm2 == "count one two three"

    def test_punctuation_and_whitespace_collapse(self):
        norm = normalize_text_for_option_b("   What   tasks,   are waiting?   ")
        assert norm == "what tasks are waiting"


# ==============================================================================
# 2. Audio File Safety, Format, and Cryptographic Checks
# ==============================================================================

class TestOptionBAudioFileValidation:
    """Validates local WAV file safety, format, and SHA-256 checks."""

    @pytest.fixture
    def corpus_env(self, tmp_path: Path):
        corpus_dir = tmp_path / "option_b_audio"
        corpus_dir.mkdir()
        wav_bytes = create_test_wav_bytes(duration_s=1.5, sample_rate=16000, channels=1, sampwidth=2)
        clip_path = corpus_dir / "valid_clip.wav"
        clip_path.write_bytes(wav_bytes)

        clip_meta = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="valid_clip.wav",
            sha256_digest=hashlib.sha256(wav_bytes).hexdigest().lower(),
            size_bytes=len(wav_bytes),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.5,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        return corpus_dir, clip_meta, wav_bytes

    def test_valid_audio_file_accepted(self, corpus_env):
        corpus_dir, clip_meta, wav_bytes = corpus_env
        val = validate_option_b_audio_file(clip_meta, corpus_dir)
        assert val.path.exists()
        assert val.path.name == "valid_clip.wav"
        assert len(val.audio_bytes) == len(wav_bytes)

    def test_missing_file_rejected(self, corpus_env):
        corpus_dir, clip_meta, _ = corpus_env
        (corpus_dir / "valid_clip.wav").unlink()
        with pytest.raises(OptionBAudioValidationError, match="audio file not found"):
            validate_option_b_audio_file(clip_meta, corpus_dir)

    def test_file_size_mismatch_rejected(self, corpus_env):
        corpus_dir, clip_meta, wav_bytes = corpus_env
        # Truncate file
        (corpus_dir / "valid_clip.wav").write_bytes(wav_bytes[:-10])
        with pytest.raises(OptionBAudioValidationError, match="File size mismatch"):
            validate_option_b_audio_file(clip_meta, corpus_dir)

    def test_sha256_digest_mismatch_rejected(self, corpus_env):
        corpus_dir, clip_meta, wav_bytes = corpus_env
        # Mutate last byte without changing length
        corrupted = bytearray(wav_bytes)
        corrupted[-1] = (corrupted[-1] + 1) % 256
        (corpus_dir / "valid_clip.wav").write_bytes(bytes(corrupted))
        with pytest.raises(OptionBAudioValidationError, match="SHA-256 digest mismatch"):
            validate_option_b_audio_file(clip_meta, corpus_dir)

    def test_wrong_sample_rate_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # 44.1 kHz WAV
        wrong_wav = create_test_wav_bytes(sample_rate=44100)
        (corpus_dir / "wrong_sr.wav").write_bytes(wrong_wav)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="wrong_sr.wav",
            sha256_digest=hashlib.sha256(wrong_wav).hexdigest().lower(),
            size_bytes=len(wrong_wav),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="Audio sample rate violation.*expected 16000 Hz, got 44100 Hz"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_stereo_channels_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # Stereo WAV
        stereo_wav = create_test_wav_bytes(channels=2)
        (corpus_dir / "stereo.wav").write_bytes(stereo_wav)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="stereo.wav",
            sha256_digest=hashlib.sha256(stereo_wav).hexdigest().lower(),
            size_bytes=len(stereo_wav),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="Audio channel format violation.*expected mono.*got 2"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_wrong_bit_depth_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # 8-bit WAV (sampwidth=1)
        audio_8bit = create_test_wav_bytes(sampwidth=1, fill_byte=b"\x80")
        (corpus_dir / "8bit.wav").write_bytes(audio_8bit)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="8bit.wav",
            sha256_digest=hashlib.sha256(audio_8bit).hexdigest().lower(),
            size_bytes=len(audio_8bit),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="Audio bit depth violation.*strictly 16-bit PCM, got 8-bit"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_malformed_wav_header_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        corrupt_bytes = b"RIFF1234WAVEfmt not_a_real_wav_header_at_all"
        (corpus_dir / "corrupt.wav").write_bytes(corrupt_bytes)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="corrupt.wav",
            sha256_digest=hashlib.sha256(corrupt_bytes).hexdigest().lower(),
            size_bytes=len(corrupt_bytes),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="Audio file 'corrupt.wav' (RIFF chunk size|missing|too short)"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_symlink_escaping_corpus_root_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        outside_file = tmp_path / "outside.wav"
        wav_bytes = create_test_wav_bytes(1.0)
        outside_file.write_bytes(wav_bytes)

        symlink_path = corpus_dir / "symlink_escape.wav"
        try:
            symlink_path.symlink_to(outside_file)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this Windows environment")

        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="symlink_escape.wav",
            sha256_digest=hashlib.sha256(wav_bytes).hexdigest().lower(),
            size_bytes=len(wav_bytes),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="resolves outside corpus root"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_rejection_of_known_synthetic_smoke_or_historical_digest(self, tmp_path: Path, monkeypatch):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        from tools.bench.option_b_eval import KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS
        known_digest = next(iter(KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS))
        wav_bytes = create_test_wav_bytes(1.0)
        (corpus_dir / "jfk_fake.wav").write_bytes(wav_bytes)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="jfk_fake.wav",
            sha256_digest=known_digest,
            size_bytes=len(wav_bytes),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        mock_hasher = MagicMock()
        mock_hasher.hexdigest.return_value = known_digest
        monkeypatch.setattr("hashlib.sha256", lambda *args, **kwargs: mock_hasher)
        with pytest.raises(OptionBAudioValidationError, match="Security rejection.*matches known synthetic smoke or historical prototype digest"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_pcm_payload_incomplete_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)
        data = bytearray(buf.getvalue())
        data = data[:-100]  # truncate payload
        (corpus_dir / "truncated.wav").write_bytes(bytes(data))
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="truncated.wav",
            sha256_digest=hashlib.sha256(data).hexdigest().lower(),
            size_bytes=len(data),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="PCM payload incomplete|Failed to parse|exceeds actual file length"):
            validate_option_b_audio_file(clip, corpus_dir)

    def test_duration_mismatch_rejected(self, corpus_env):
        corpus_dir, clip_meta, wav_bytes = corpus_env
        mismatched_clip = OptionBClipManifest(
            command_id=clip_meta.command_id,
            filename=clip_meta.filename,
            sha256_digest=clip_meta.sha256_digest,
            size_bytes=clip_meta.size_bytes,
            sample_rate_hz=clip_meta.sample_rate_hz,
            channels=clip_meta.channels,
            sample_format=clip_meta.sample_format,
            duration_seconds=3.0,  # manifest claims 3.0s vs 1.5s actual
            consenting_speaker_id=clip_meta.consenting_speaker_id,
            physical_microphone_model=clip_meta.physical_microphone_model,
            acoustic_environment_description=clip_meta.acoustic_environment_description,
            ground_truth_transcript=clip_meta.ground_truth_transcript,
        )
        with pytest.raises(OptionBAudioValidationError, match="Duration mismatch"):
            validate_option_b_audio_file(mismatched_clip, corpus_dir)

    def test_12bit_wav_header_rejected(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        # Crafted 12-bit WAV with sampwidth=2 in wave module
        wav_12bit = create_12bit_test_wav_bytes(duration_s=1.0)
        (corpus_dir / "12bit.wav").write_bytes(wav_12bit)
        clip = OptionBClipManifest(
            command_id="cmd_short_1s",
            filename="12bit.wav",
            sha256_digest=hashlib.sha256(wav_12bit).hexdigest().lower(),
            size_bytes=len(wav_12bit),
            sample_rate_hz=16000,
            channels=1,
            sample_format="16-bit signed PCM (little-endian)",
            duration_seconds=1.0,
            consenting_speaker_id="operator_01",
            physical_microphone_model="SM7B",
            acoustic_environment_description="Room",
            ground_truth_transcript="PICO stop.",
        )
        with pytest.raises(OptionBAudioValidationError, match="expected strictly 16-bit PCM, got 12-bit"):
            validate_option_b_audio_file(clip, corpus_dir)


# ==============================================================================
# 3. Exact WER Arithmetic and GATE-VOICE-02 Decision Logic
# ==============================================================================

class TestOptionBGate02Evaluation:
    """Validates exact arithmetic, threshold boundaries, and gate decisions."""

    @pytest.fixture
    def setup_corpus(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        durations = {
            "cmd_short_1s": 1.0,
            "cmd_query_3s": 3.0,
            "cmd_workflow_5s": 5.0,
            "cmd_dictation_10s": 10.0,
        }

        clips_meta = []
        for cid in ["cmd_short_1s", "cmd_query_3s", "cmd_workflow_5s", "cmd_dictation_10s"]:
            fn = f"{cid}.wav"
            wav = create_test_wav_bytes(duration_s=durations[cid])
            (corpus_dir / fn).write_bytes(wav)
            clips_meta.append(create_mock_clip_entry(cid, fn, wav, duration_s=durations[cid]))

        manifest_data = create_mock_manifest_dict(clips=clips_meta)
        manifest_path = tmp_path / "voice_corpus_manifest.json"
        manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")

        return manifest_path, corpus_dir

    def test_gate_02_zero_errors_passes(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()

        # Engine produces exact ground truth text
        def mock_transcribe(wav_bytes, n_threads=4):
            # return exact text based on command
            return "dummy"

        mock_engine.transcribe_wav_bytes.side_effect = [
            "PICO stop.",
            "What tasks are currently waiting for my approval?",
            "Summarize the latest system logs and create a new draft task for review.",
            (
                "We need to verify the database migrations, ensure all foreign key constraints are enabled, "
                "and check that the memory service cleans up expired working memories automatically."
            ),
        ]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
            n_threads=4,
        )

        assert res.status == OptionBGateStatus.PASS.value
        assert res.aggregate_edit_count == 0
        assert res.exact_reference_denominator == 49
        assert res.aggregate_wer_pct == "0.0%"
        assert len(res.command_results) == 4

    def test_gate_02_boundary_five_edits_passes(self, setup_corpus):
        """5 edits / 49 words = 10.204% <= 12.0% -> PASS."""
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()

        # Generate exactly 5 word substitutions in dictation using alphabetic words (no digits)
        dictation_words = EXPECTED_CANONICAL_COMMANDS["cmd_dictation_10s"]["canonical_text"].split()
        sub_words = ["alpha", "bravo", "charlie", "delta", "echo"]
        for idx in range(5):
            dictation_words[idx] = sub_words[idx]
        dictation_with_5_subs = " ".join(dictation_words)

        mock_engine.transcribe_wav_bytes.side_effect = [
            "PICO stop.",
            "What tasks are currently waiting for my approval?",
            "Summarize the latest system logs and create a new draft task for review.",
            dictation_with_5_subs,
        ]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
            n_threads=4,
        )

        assert res.status == OptionBGateStatus.PASS.value
        assert res.aggregate_edit_count == 5
        assert res.exact_reference_denominator == 49
        # 5 / 49 = 0.10204... -> 10.2%
        assert res.aggregate_wer_pct == "10.2%"

    def test_gate_02_boundary_six_edits_fails(self, setup_corpus):
        """6 edits / 49 words = 12.245% > 12.0% -> FAIL."""
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()

        # Generate exactly 6 word substitutions in dictation using alphabetic words (no digits)
        dictation_words = EXPECTED_CANONICAL_COMMANDS["cmd_dictation_10s"]["canonical_text"].split()
        sub_words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
        for idx in range(6):
            dictation_words[idx] = sub_words[idx]
        dictation_with_6_subs = " ".join(dictation_words)

        mock_engine.transcribe_wav_bytes.side_effect = [
            "PICO stop.",
            "What tasks are currently waiting for my approval?",
            "Summarize the latest system logs and create a new draft task for review.",
            dictation_with_6_subs,
        ]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
            n_threads=4,
        )

        assert res.status == OptionBGateStatus.FAIL.value
        assert res.aggregate_edit_count == 6
        assert res.exact_reference_denominator == 49
        # 6 / 49 = 0.12244... -> 12.24%
        assert res.aggregate_wer_pct == "12.24%"

    def test_gate_02_more_than_six_edits_fails(self, setup_corpus):
        """10 edits / 49 words = 20.41% > 12.0% -> FAIL."""
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()

        dictation_words = EXPECTED_CANONICAL_COMMANDS["cmd_dictation_10s"]["canonical_text"].split()
        sub_words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel", "india", "juliet"]
        for idx in range(10):
            dictation_words[idx] = sub_words[idx]
        dictation_with_10_subs = " ".join(dictation_words)

        mock_engine.transcribe_wav_bytes.side_effect = [
            "PICO stop.",
            "What tasks are currently waiting for my approval?",
            "Summarize the latest system logs and create a new draft task for review.",
            dictation_with_10_subs,
        ]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
            n_threads=4,
        )

        assert res.status == OptionBGateStatus.FAIL.value
        assert res.aggregate_edit_count == 10
        assert res.exact_reference_denominator == 49
        assert res.aggregate_wer_pct == "20.41%"

    def test_gate_02_empty_transcript_fails_as_measured_result(self, setup_corpus):
        """Empty transcript yields 49 deletions -> 49 edits / 49 words = 100% -> FAIL (measured)."""
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()
        mock_engine.transcribe_wav_bytes.side_effect = ["", "", "", ""]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
            n_threads=4,
        )

        assert res.status == OptionBGateStatus.FAIL.value
        assert res.aggregate_edit_count == 49
        assert res.aggregate_wer_pct == "100.0%"

    def test_gate_02_missing_clips_returns_not_run(self, tmp_path: Path):
        """Manifest with clips: [] returns NOT RUN with reason PENDING_RECORDINGS."""
        manifest_data = create_mock_manifest_dict(clips=[])
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=tmp_path,
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "clips array is empty" in res.reason

    def test_gate_02_missing_corpus_dir_returns_not_run(self, setup_corpus):
        manifest_path, _ = setup_corpus

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=None,
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "not provided" in res.reason

    def test_gate_02_runtime_unavailable_returns_not_run(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus

        res = evaluate_option_b_gate_02(
            engine=None,  # Engine unavailable
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "PicoWhisperEngine is not initialized or unavailable" in res.reason

    def test_gate_02_inference_crash_returns_not_run_not_fail(self, setup_corpus):
        """Inference execution crash must be NOT RUN with reason, never misreported as accuracy FAIL."""
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()
        mock_engine.transcribe_wav_bytes.side_effect = RuntimeError("Native segmentation fault in whisper shim")

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "Inference execution failed" in res.reason
        assert "Native segmentation fault" in res.reason

    def test_pending_recordings_with_clips_returns_not_run(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        manifest_data["formal_accuracy_corpus"]["status"] = "PENDING_RECORDINGS"
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "PENDING_RECORDINGS" in res.reason and "not qualifying" in res.reason

    def test_duplicate_physical_file_alias_rejected(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        # Point command 4 to the same physical file as command 1 via relative path alias
        clips = manifest_data["formal_accuracy_corpus"]["clips"]
        clips[3]["filename"] = "./" + clips[0]["filename"]
        clips[3]["size_bytes"] = clips[0]["size_bytes"]
        clips[3]["duration_seconds"] = clips[0]["duration_seconds"]
        clips[3]["sha256_digest"] = clips[0]["sha256_digest"]
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert ("Duplicate file alias detected" in res.reason or "Duplicate audio content detected" in res.reason)

    def test_tuple_engine_return_unpacked_properly(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        mock_engine = MagicMock()
        # PicoWhisperEngine.transcribe_wav_bytes returns (transcript, elapsed, duration)
        mock_engine.transcribe_wav_bytes.side_effect = [
            ("PICO stop.", 0.1, 1.0),
            ("What tasks are currently waiting for my approval?", 0.2, 1.0),
            ("Summarize the latest system logs and create a new draft task for review.", 0.3, 1.0),
            ("We need to verify the database migrations, ensure all foreign key constraints are enabled, and check that the memory service cleans up expired working memories automatically.", 0.5, 1.0),
        ]

        res = evaluate_option_b_gate_02(
            engine=mock_engine,
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.PASS.value
        assert res.aggregate_edit_count == 0
        assert res.aggregate_wer_pct == "0.0%"

    def test_duplicate_audio_digest_across_clips_rejected(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        # Give two different filenames the exact same sha256_digest
        clips = manifest_data["formal_accuracy_corpus"]["clips"]
        clips[1]["sha256_digest"] = clips[0]["sha256_digest"]
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "Duplicate audio content detected" in res.reason

    def test_symlink_audio_file_rejected_in_gate_02(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        # Replace one clip with a symlink
        clip_path = corpus_dir / "cmd_short_1s.wav"
        target_path = corpus_dir / "real_cmd_short_1s.wav"
        clip_path.rename(target_path)
        try:
            clip_path.symlink_to(target_path)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation not permitted in this Windows environment")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "Symlink rejected" in res.reason

    def test_hardlink_audio_file_rejected_in_gate_02(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)

        src_path = corpus_dir / "cmd_short_1s.wav"
        link_path = corpus_dir / "cmd_query_3s_hardlink.wav"
        try:
            os.link(src_path, link_path)
        except (OSError, NotImplementedError):
            pytest.skip("Hardlink creation not permitted in this Windows environment")

        clips = manifest_data["formal_accuracy_corpus"]["clips"]
        clips[1]["filename"] = link_path.name
        clips[1]["size_bytes"] = len(src_path.read_bytes())
        clips[1]["sha256_digest"] = hashlib.sha256(src_path.read_bytes()).hexdigest().lower()
        clips[1]["duration_seconds"] = clips[0]["duration_seconds"]
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert ("Duplicate physical file detected" in res.reason or "Duplicate audio content detected" in res.reason)

    def test_privacy_no_absolute_paths_in_result_or_report(self, tmp_path: Path):
        """Ensure absolute paths, machine usernames, and directory trees are never leaked."""
        secret_dir = tmp_path / "C_Users_SecretDeveloper_PrivateTree"
        secret_dir.mkdir()
        manifest_path = secret_dir / "voice_corpus_manifest.json"
        corpus_dir = secret_dir / "private_audio_vault"
        corpus_dir.mkdir()

        # Non-existent or error cases
        res = evaluate_option_b_gate_02(
            engine=None,
            manifest_path=secret_dir / "nonexistent_manifest.json",
            corpus_dir=corpus_dir,
        )
        assert str(secret_dir) not in res.reason
        assert "SecretDeveloper" not in res.reason
        res_dict = res.to_dict()
        assert "manifest_path" not in res_dict  # Removed in favor of display name and digest
        assert str(secret_dir) not in str(res_dict)

        # Test report rendering
        report = {
            "timestamp": "2026-11-21T00:00:00Z",
            "host": {
                "processor": "Intel", "cpu_topology": "8C", "cpu_threads_dedicated": 4,
                "git_commit": "abcdef", "git_clean": True,
            },
            "runtime": {"version": "v1.9.5"},
            "model": {"filename": "model.bin", "sha256": "1234567890abcdef"},
            "gates": {
                "GATE-VOICE-02": {
                    "name": "STT Accuracy",
                    "threshold": "<= 12.0%",
                    "measured": res.format_measured_summary(),
                    "status": "NOT RUN",
                }
            },
            "results": [],
            "summary": {},
            "option_b_gate_02": res.to_dict(),
        }
        md = voice_bench.render_markdown_report(report)
        assert str(secret_dir) not in md
        assert "SecretDeveloper" not in md

    def test_populated_manifest_with_synthetic_or_historical_digest_rejected(self, setup_corpus):
        manifest_path, corpus_dir = setup_corpus
        from tools.bench.option_b_eval import KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS
        known_digest = next(iter(KNOWN_NON_HUMAN_OR_HISTORICAL_DIGESTS))

        with manifest_path.open("r", encoding="utf-8") as f:
            manifest_data = json.load(f)
        # Put known synthetic digest into first clip
        clips = manifest_data["formal_accuracy_corpus"]["clips"]
        clips[0]["sha256_digest"] = known_digest
        manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=corpus_dir,
        )
        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "Audio validation failed" in res.reason


# ==============================================================================
# 4. Benchmark Integration and Strict Gate Segregation
# ==============================================================================

class TestOptionBBenchmarkIntegration:
    """Verifies integration into voice_bench.py and dataset segregation."""

    def test_synthetic_sapi_clips_cannot_produce_gate_02_pass(self, tmp_path: Path):
        """Synthetic SAPI clips in smoke_test_clips must NEVER satisfy GATE-VOICE-02."""
        # Setup mock corpus dir with only SAPI smoke WAVs
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        for fname in ["audio_cmd_short_1s.wav", "audio_cmd_query_3s.wav", "audio_cmd_workflow_5s.wav", "audio_cmd_dictation_10s.wav"]:
            (corpus_dir / fname).write_bytes(create_test_wav_bytes(4.92))

        # Real speech (historical JFK) absent
        report = {
            "timestamp": "2026-11-21T00:00:00Z",
            "host": {"processor": "Intel", "cpu_topology": "8C", "cpu_threads_dedicated": 4},
            "runtime": {"version": "v1.9.5"},
            "model": {"filename": "model.bin"},
            "gates": {},
            "results": [],
            "summary": {
                "real_speech_aggregate_wer": None,
                "real_speech_macro_wer": None,
                "real_speech_macro_cer": None,
                "smoke_commands_aggregate_wer": 0.0,
                "smoke_commands_macro_wer": 0.0,
            },
        }

        # Empty Option B manifest
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(create_mock_manifest_dict(clips=[])), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=None,  # No Option B corpus provided
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value
        assert "clips array is empty" in res.reason

        report["option_b_gate_02"] = res.to_dict()
        report["gates"]["GATE-VOICE-02"] = {
            "name": "STT Accuracy (WER)",
            "threshold": "Aggregate WER <= 12.0%",
            "measured": res.format_measured_summary(smoke_wer=0.0),
            "status": res.status,
        }

        md = voice_bench.render_markdown_report(report)
        assert "| `GATE-VOICE-02` | **STT Accuracy (WER)** | Aggregate WER <= 12.0% |" in md
        assert "NOT RUN" in md
        assert "Synthetic Commands Smoke WER**: 0.0%" in md

    def test_historical_jfk_vosk_fixtures_cannot_produce_gate_02_pass(self, tmp_path: Path):
        """Historical JFK + Vosk fixtures (75 words, 2.67% WER) must not produce GATE-VOICE-02 PASS."""
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(create_mock_manifest_dict(clips=[])), encoding="utf-8")

        res = evaluate_option_b_gate_02(
            engine=MagicMock(),
            manifest_path=manifest_path,
            corpus_dir=None,
        )

        assert res.status == OptionBGateStatus.NOT_RUN.value

        measured = res.format_measured_summary(
            historical_mixed_wer=0.0267,
            historical_macro_wer=0.0833,
            historical_macro_cer=0.0641,
        )
        assert "2.67%" in measured
        assert "5 JFK slices + 1 Vosk digit sequence, 75 words, 2 edits" in measured
        assert "NOT RUN" in measured

    def test_run_benchmark_focused_lifecycle_integration(self, tmp_path: Path, monkeypatch):
        """Verify run_benchmark orchestrates engine lifecycle, active sampler, Ollama checkpoints, and gate results."""
        model_file = tmp_path / "ggml-base.en.bin"
        model_file.write_bytes(b"GGUF" + b"\x00" * 100)

        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        wav_bytes = create_test_wav_bytes(duration_s=4.92)
        (corpus_dir / "audio_cmd_workflow_5s.wav").write_bytes(wav_bytes)

        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps(create_mock_manifest_dict(clips=[])), encoding="utf-8")

        mock_engine_instance = MagicMock()
        mock_engine_instance.version = "v1.9.5"
        mock_engine_instance.system_info = "AVX2"
        mock_engine_instance.transcribe_wav_bytes.return_value = (
            "summarize the latest system logs", 0.2, 4.92,
            {
                "wav_parse_seconds": 0.001,
                "pcm_norm_seconds": 0.002,
                "c_buf_prep_seconds": 0.001,
                "native_inference_seconds": 0.196,
            }
        )
        monkeypatch.setattr(voice_bench, "PicoWhisperEngine", lambda **kw: mock_engine_instance)
        monkeypatch.setattr(voice_bench, "verify_model_artifact", lambda p: {"filename": p.name, "sha256": "abcdef"})

        from tools.bench.voice_bench import OllamaResidencyResult, OllamaResidencyState
        res_mock = OllamaResidencyResult(OllamaResidencyState.GPU_RESIDENT, "qwen3.5:2b is resident on GPU", "digest123")
        monkeypatch.setattr(voice_bench, "verify_ollama_model_residency", lambda m: res_mock)

        monkeypatch.setattr(voice_bench, "query_gpu_vram_mb", lambda: 1200)
        monkeypatch.setattr(voice_bench, "query_process_memory_mb", lambda: (250.0, 200.0, 150.0))
        monkeypatch.setattr(voice_bench, "query_system_memory_mb", lambda: (16000.0, 8000.0))
        monkeypatch.setattr(voice_bench, "audit_active_process_sockets", lambda pid: [])

        mock_sampler = MagicMock()
        mock_sampler.get_results.return_value = (300.0, 7500.0, True, 20)
        monkeypatch.setattr(voice_bench, "ContinuousMemorySampler", lambda **kw: mock_sampler)

        report = voice_bench.run_benchmark(
            model_path=model_file,
            corpus_dir=corpus_dir,
            runtime_dir=tmp_path / "runtime",
            manifest_path=manifest_path,
            option_b_corpus_dir=None,
            iterations=1,
            n_threads=4,
        )

        assert report["host"]["ollama_llm_resident"] is True
        assert mock_sampler.start.called
        assert mock_sampler.stop.called
        assert mock_engine_instance.close.called
        assert "option_b_gate_02" in report
        assert report["option_b_gate_02"]["status"] == "NOT RUN"
        assert report["gates"]["GATE-VOICE-01"]["status"] in ("PASS", "FAIL")
