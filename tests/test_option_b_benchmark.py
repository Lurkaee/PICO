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
import os
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
) -> dict:
    """Helper to generate a valid clip manifest entry matching generated audio bytes."""
    gt = ground_truth or EXPECTED_CANONICAL_COMMANDS[command_id]["canonical_text"]
    sha = hashlib.sha256(audio_bytes).hexdigest().lower()
    return {
        "command_id": command_id,
        "filename": filename,
        "sha256_digest": sha,
        "size_bytes": len(audio_bytes),
        "sample_rate_hz": 16000,
        "channels": 1,
        "sample_format": "16-bit signed PCM (little-endian)",
        "duration_seconds": 1.5,
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
        corpus_dir, clip_meta, _ = corpus_env
        path = validate_option_b_audio_file(clip_meta, corpus_dir)
        assert path.exists()
        assert path.name == "valid_clip.wav"

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
        with pytest.raises(OptionBAudioValidationError, match="Audio bit depth violation.*expected 16-bit.*got 1"):
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
        with pytest.raises(OptionBAudioValidationError, match="Failed to parse RIFF/WAVE header"):
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


# ==============================================================================
# 3. Exact WER Arithmetic and GATE-VOICE-02 Decision Logic
# ==============================================================================

class TestOptionBGate02Evaluation:
    """Validates exact arithmetic, threshold boundaries, and gate decisions."""

    @pytest.fixture
    def setup_corpus(self, tmp_path: Path):
        corpus_dir = tmp_path / "corpus"
        corpus_dir.mkdir()
        wav = create_test_wav_bytes(1.0)

        clips_meta = []
        for cid in ["cmd_short_1s", "cmd_query_3s", "cmd_workflow_5s", "cmd_dictation_10s"]:
            fn = f"{cid}.wav"
            (corpus_dir / fn).write_bytes(wav)
            clips_meta.append(create_mock_clip_entry(cid, fn, wav))

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
