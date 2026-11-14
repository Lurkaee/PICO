"""Tests proving that Alembic migrations run deterministically during PICO Core startup (P11.3.1).

Validates three specific database states:
A. Fresh database:
   - Start PICO Core via standard application lifespan.
   - Confirm all tables and indexes exist.
   - Confirm the partial unique index uq_perception_events_voice_session exists.

B. Existing database:
   - Construct a database at the previous migration revision (b2c3d4e5f6a7).
   - Insert representative existing conversation, message, and perception data.
   - Start PICO Core via standard application lifespan with apply_pending_migrations.
   - Confirm migrations execute correctly to head (c3d4e5f6a7b8).
   - Verify all pre-existing data remains intact and accessible.
   - Verify the new partial unique index exists.

C. Restart and replay idempotency:
   - Deliver a transcript to conversation.
   - Restart the Core using standard lifespan.
   - Attempt delivery again using the same session identifier.
   - Confirm the duplicate is rejected by the database constraint and no duplicate message is created.
"""

from __future__ import annotations

import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from pico.core.config import settings
from pico.db.base import Base
from pico.db.session import (
    _canonicalize_sqlite_db_path,
    _migration_file_lock,
    apply_pending_migrations,
)
from pico.main import app
from pico.models.conversation import Conversation
from pico.models.message import Message
from pico.models.operator_session import OperatorSessionModel
from pico.models.perception_event import PerceptionEvent
from pico.perception.audio.adapter import (
    AudioCaptureState,
    AudioSessionManager,
    MockSpeechToTextAdapter,
)
from pico.services.voice_service import VoiceService


def test_database_startup_fresh_database() -> None:
    """State A: Fresh database initialized via normal application startup has all tables and the uq index."""
    temp_dir = tempfile.mkdtemp(prefix="pico_fresh_db_")
    try:
        db_path = Path(temp_dir) / "fresh.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # Execute migration runner as invoked during app lifespan
        apply_pending_migrations(db_url)

        engine = create_engine(db_url)
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "c3d4e5f6a7b8"

        insp = inspect(engine)
        tables = insp.get_table_names()
        assert "conversations" in tables
        assert "messages" in tables
        assert "perception_events" in tables

        indexes = [idx["name"] for idx in insp.get_indexes("perception_events")]
        assert "uq_perception_events_voice_session" in indexes
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_existing_database_upgrade() -> None:
    """State B: Existing database at b2c3d4e5f6a7 with existing data is upgraded cleanly to head."""
    temp_dir = tempfile.mkdtemp(prefix="pico_existing_db_")
    try:
        db_path = Path(temp_dir) / "existing.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # 1. Provision database at previous revision
        ini_path = Path(__file__).resolve().parents[1] / "alembic.ini"
        cfg = Config(str(ini_path))
        cfg.set_main_option("sqlalchemy.url", db_url)
        command.upgrade(cfg, "b2c3d4e5f6a7")

        engine = create_engine(db_url)
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "b2c3d4e5f6a7"

        # Verify index is NOT present yet
        insp = inspect(engine)
        indexes_before = [idx["name"] for idx in insp.get_indexes("perception_events")]
        assert "uq_perception_events_voice_session" not in indexes_before

        # 2. Insert representative historical data
        Session = sessionmaker(bind=engine)
        with Session() as s:
            conv = Conversation(id="conv_pre_upgrade", title="Historical Conversation")
            msg = Message(
                id="msg_pre_upgrade",
                conversation_id="conv_pre_upgrade",
                role="user",
                content="Historical user prompt",
                data_classification="INTERNAL",
            )
            pe = PerceptionEvent(
                id="pe_pre_upgrade",
                correlation_id="corr_pre",
                modality="audio",
                event_type="ambient_noise_sample",
                source_device_id="mic:local",
                data_classification="INTERNAL",
                confidence=1.0,
                raw_payload_scrubbed=True,
                structured_data={"db_level": 42.0},
                provenance="historical",
                retention_policy="short_term_audit",
                session_id="sess_pre_historical",
            )
            s.add(conv)
            s.add(msg)
            s.add(pe)
            s.commit()

        # 3. Simulate PICO normal startup running migrations on existing database
        apply_pending_migrations(db_url)

        # 4. Verify upgraded state
        engine2 = create_engine(db_url)
        with engine2.connect() as conn:
            rev_after = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev_after == "c3d4e5f6a7b8"

        insp2 = inspect(engine2)
        indexes_after = [idx["name"] for idx in insp2.get_indexes("perception_events")]
        assert "uq_perception_events_voice_session" in indexes_after

        # 5. Verify all historical data remains intact
        with Session() as s:
            loaded_conv = s.get(Conversation, "conv_pre_upgrade")
            assert loaded_conv is not None
            assert loaded_conv.title == "Historical Conversation"

            loaded_msg = s.get(Message, "msg_pre_upgrade")
            assert loaded_msg is not None
            assert loaded_msg.content == "Historical user prompt"

            loaded_pe = s.get(PerceptionEvent, "pe_pre_upgrade")
            assert loaded_pe is not None
            assert loaded_pe.structured_data["db_level"] == 42.0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_restart_and_replay_rejection() -> None:
    """State C: Deliver a transcript, simulate Core restart, and confirm duplicate replay is rejected."""
    temp_dir = tempfile.mkdtemp(prefix="pico_restart_db_")
    try:
        db_path = Path(temp_dir) / "restart.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # Initialize to head
        apply_pending_migrations(db_url)

        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)

        with Session() as s:
            conv = Conversation(id="conv_restart_test", title="Restart Test Conv")
            s.add(conv)
            s.commit()

        # Phase 1: Deliver transcript in Process 1
        mgr1 = AudioSessionManager()
        mgr1.stt_adapter = MockSpeechToTextAdapter(default_transcription="Spoken text before restart")
        start1 = mgr1.start_session(user_consent=True, operator_id="op_restart")
        sess1 = mgr1.get_session(start1.session_id)
        assert sess1 is not None
        sess1.state = AudioCaptureState.COMPLETED
        sess1.transcription = "Spoken text before restart"

        with Session() as s:
            svc1 = VoiceService(audio_manager=mgr1)
            res1 = svc1.deliver_transcript_to_conversation(
                session_id=start1.session_id,
                conversation_id="conv_restart_test",
                db=s,
                caller_operator_id="op_restart",
            )
            assert res1["delivered"] is True

        # Phase 2: Simulate Core process restart
        # 1. Migrations re-run (idempotent no-op)
        apply_pending_migrations(db_url)

        # 2. Fresh AudioSessionManager has clean memory
        mgr2 = AudioSessionManager()
        mgr2.stt_adapter = MockSpeechToTextAdapter(default_transcription="Spoken text before restart")
        start2 = mgr2.start_session(user_consent=True, operator_id="op_restart")
        sess2 = mgr2.get_session(start2.session_id)
        assert sess2 is not None

        # Replay the identical session_id from Process 1
        replayed_sess_id = start1.session_id
        mgr2._active_sessions[replayed_sess_id] = sess2
        sess2.session_id = replayed_sess_id
        sess2.state = AudioCaptureState.COMPLETED
        sess2.transcription = "Spoken text before restart"
        sess2.delivered = False  # fresh in-memory state

        with Session() as s:
            svc2 = VoiceService(audio_manager=mgr2)
            with pytest.raises(Exception, match="database integrity constraint"):
                svc2.deliver_transcript_to_conversation(
                    session_id=replayed_sess_id,
                    conversation_id="conv_restart_test",
                    db=s,
                    caller_operator_id="op_restart",
                )

        # Confirm exactly 1 message and 1 delivery event in DB
        with Session() as s:
            msgs = s.scalars(select(Message).where(Message.conversation_id == "conv_restart_test")).all()
            assert len(msgs) == 1
            assert msgs[0].content == "Spoken text before restart"

            events = s.scalars(
                select(PerceptionEvent).where(
                    PerceptionEvent.session_id == replayed_sess_id,
                    PerceptionEvent.event_type == "voice_transcript_delivery",
                )
            ).all()
            assert len(events) == 1
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_unversioned_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """State D: Legacy unversioned PICO database created via Base.metadata.create_all() prior to P11.3.1.

    Requirements:
    1. Construct the old unversioned database using Base.metadata.create_all().
    2. Drop uq_perception_events_voice_session if present to faithfully reproduce pre-c3d4e5f6a7b8 schema.
    3. Verify alembic_version table is absent.
    4. Insert representative conversations, messages, perception events, and operator sessions.
    5. Start Core through the actual application lifespan.
    6. Confirm legacy schema recognition and stamping to baseline (b2c3d4e5f6a7).
    7. Confirm pending migrations execute to head (c3d4e5f6a7b8).
    8. Confirm voice-session partial unique index exists.
    9. Confirm original records remain readable and unchanged.
    10. Attempt duplicate transcript delivery after migration and confirm it is rejected.
    """
    temp_dir = tempfile.mkdtemp(prefix="pico_legacy_db_")
    try:
        db_path = Path(temp_dir) / "legacy.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # 1. Construct the unversioned database using the old startup implementation (Base.metadata.create_all)
        engine_init = create_engine(db_url)
        Base.metadata.create_all(bind=engine_init)
        with engine_init.connect() as conn:
            # Faithfully match pre-c3d4e5f6a7b8 schema by dropping the index if created from modern model definitions
            conn.execute(text("DROP INDEX IF EXISTS uq_perception_events_voice_session"))
            conn.commit()

        # 2. Verify alembic_version is absent
        insp_init = inspect(engine_init)
        assert "alembic_version" not in insp_init.get_table_names()
        assert "conversations" in insp_init.get_table_names()
        assert "perception_events" in insp_init.get_table_names()
        assert "operator_sessions" in insp_init.get_table_names()
        indexes_init = [idx["name"] for idx in insp_init.get_indexes("perception_events")]
        assert "uq_perception_events_voice_session" not in indexes_init

        # 3. Insert representative historical data
        Session = sessionmaker(bind=engine_init)
        now = datetime.now(timezone.utc)
        with Session() as s:
            conv = Conversation(id="conv_legacy_001", title="Legacy Conversation Title")
            msg = Message(
                id="msg_legacy_001",
                conversation_id="conv_legacy_001",
                role="user",
                content="Historical unversioned prompt",
                data_classification="INTERNAL",
            )
            pe = PerceptionEvent(
                id="pe_legacy_001",
                correlation_id="corr_legacy_001",
                modality="audio",
                event_type="ambient_noise_sample",
                source_device_id="mic:host",
                data_classification="INTERNAL",
                confidence=0.9,
                raw_payload_scrubbed=True,
                structured_data={"noise_floor": -45.0},
                provenance="sensor",
                retention_policy="short_term_audit",
                session_id="sess_legacy_001",
            )
            op = OperatorSessionModel(
                id="sess_op_legacy",
                approver="operator_local",
                csrf_token="csrf_legacy_secret",
                is_active=True,
                expires_at=now + timedelta(hours=24),
            )
            s.add(conv)
            s.add(msg)
            s.add(pe)
            s.add(op)
            s.commit()
        engine_init.dispose()

        # 4. Start Core through the actual application lifespan
        monkeypatch.setattr(settings, "DATABASE_URL", db_url)
        with TestClient(app) as client:
            resp = client.get("/health")
            assert resp.status_code == 200

        # 5. Confirm migration to head revision
        engine_post = create_engine(db_url)
        with engine_post.connect() as conn:
            rev_post = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev_post == "c3d4e5f6a7b8"

        # 6. Confirm voice-session partial unique index exists
        insp_post = inspect(engine_post)
        indexes_post = [idx["name"] for idx in insp_post.get_indexes("perception_events")]
        assert "uq_perception_events_voice_session" in indexes_post

        # 7. Confirm original records remain readable and unchanged
        SessionPost = sessionmaker(bind=engine_post)
        with SessionPost() as s:
            loaded_conv = s.get(Conversation, "conv_legacy_001")
            assert loaded_conv is not None
            assert loaded_conv.title == "Legacy Conversation Title"

            loaded_msg = s.get(Message, "msg_legacy_001")
            assert loaded_msg is not None
            assert loaded_msg.content == "Historical unversioned prompt"

            loaded_pe = s.get(PerceptionEvent, "pe_legacy_001")
            assert loaded_pe is not None
            assert loaded_pe.structured_data["noise_floor"] == -45.0

            loaded_op = s.get(OperatorSessionModel, "sess_op_legacy")
            assert loaded_op is not None
            assert loaded_op.approver == "operator_local"

        # 8. Attempt duplicate transcript delivery and confirm database constraint rejects it
        mgr = AudioSessionManager()
        mgr.stt_adapter = MockSpeechToTextAdapter(default_transcription="Delivered transcript on legacy DB")
        start = mgr.start_session(user_consent=True, operator_id="operator_local")
        sess = mgr.get_session(start.session_id)
        assert sess is not None
        sess.state = AudioCaptureState.COMPLETED
        sess.transcription = "Delivered transcript on legacy DB"

        with SessionPost() as s:
            svc = VoiceService(audio_manager=mgr)
            # First delivery succeeds
            res = svc.deliver_transcript_to_conversation(
                session_id=start.session_id,
                conversation_id="conv_legacy_001",
                db=s,
                caller_operator_id="operator_local",
            )
            assert res["delivered"] is True

        # Second delivery with same session ID must fail closed via database uniqueness constraint
        sess.delivered = False
        with SessionPost() as s:
            svc = VoiceService(audio_manager=mgr)
            with pytest.raises(Exception, match="database integrity constraint"):
                svc.deliver_transcript_to_conversation(
                    session_id=start.session_id,
                    conversation_id="conv_legacy_001",
                    db=s,
                    caller_operator_id="operator_local",
                )
        engine_post.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_ambiguous_unversioned_database_fails_closed() -> None:
    """State E: An unversioned database with ambiguous/partial schema fails closed with actionable diagnostic."""
    temp_dir = tempfile.mkdtemp(prefix="pico_ambiguous_db_")
    try:
        db_path = Path(temp_dir) / "ambiguous.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # Create only a subset of tables (e.g. conversations and messages only, missing operator_sessions, etc.)
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE TABLE conversations (id VARCHAR(64) PRIMARY KEY, title VARCHAR(255), "
                    "created_at DATETIME, updated_at DATETIME, metadata_json JSON);"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE messages (id VARCHAR(64) PRIMARY KEY, conversation_id VARCHAR(64), "
                    "role VARCHAR(32), content TEXT, data_classification VARCHAR(32), created_at DATETIME);"
                )
            )
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="Ambiguous unversioned database schema detected") as exc_info:
            apply_pending_migrations(db_url)

        assert "Missing expected baseline tables" in str(exc_info.value)
        assert "operator_sessions" in str(exc_info.value)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_concurrent_migrations_safe() -> None:
    """State F: Concurrent application startup calls to apply_pending_migrations run safely without race."""
    temp_dir = tempfile.mkdtemp(prefix="pico_concurrent_db_")
    try:
        db_path = Path(temp_dir) / "concurrent.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        # Run 5 concurrent startup migrations on the same fresh database
        def run_startup() -> None:
            apply_pending_migrations(db_url)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(run_startup) for _ in range(5)]
            for f in futures:
                f.result()

        # Verify database is cleanly at head
        engine = create_engine(db_url)
        with engine.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert rev == "c3d4e5f6a7b8"
        insp = inspect(engine)
        assert "uq_perception_events_voice_session" in [idx["name"] for idx in insp.get_indexes("perception_events")]
        engine.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def _create_legacy_unversioned_db(db_url: str) -> None:
    """Helper to construct a valid unversioned legacy database matching pre-c3d4e5f6a7b8 schema."""
    engine = create_engine(db_url)
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_perception_events_voice_session"))
        conn.commit()
    engine.dispose()


def test_database_startup_legacy_missing_table_fails_closed() -> None:
    """Negative 1: An unversioned database missing a required baseline table fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_missing_tbl_")
    try:
        db_path = Path(temp_dir) / "missing_tbl.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Drop one of the required baseline tables
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE workflow_steps;"))
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="Missing expected baseline tables") as exc_info:
            apply_pending_migrations(db_url)

        assert "workflow_steps" in str(exc_info.value)

        # Assert database was NOT stamped or modified
        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_missing_column_fails_closed() -> None:
    """Negative 2: An unversioned database missing a required column fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_missing_col_")
    try:
        db_path = Path(temp_dir) / "missing_col.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Drop required column 'role' from messages table
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE messages DROP COLUMN role;"))
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="is missing required columns") as exc_info:
            apply_pending_migrations(db_url)

        assert "role" in str(exc_info.value)
        assert "messages" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_incompatible_column_type_or_nullability_fails_closed() -> None:
    """Negative 3: An unversioned database with incompatible column nullability or type fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_bad_col_")
    try:
        db_path = Path(temp_dir) / "bad_col.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Re-create messages table with nullable 'role' (expected nullable=False)
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE messages;"))
            conn.execute(
                text(
                    "CREATE TABLE messages ("
                    "id VARCHAR(64) PRIMARY KEY NOT NULL, "
                    "conversation_id VARCHAR(64) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, "
                    "role VARCHAR(32) NULL, "
                    "content TEXT NOT NULL, "
                    "data_classification VARCHAR(32) NOT NULL, "
                    "token_count INTEGER, "
                    "metadata_json JSON, "
                    "created_at DATETIME NOT NULL"
                    ");"
                )
            )
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="nullability mismatch") as exc_info:
            apply_pending_migrations(db_url)

        assert "messages.role" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_missing_foreign_key_fails_closed() -> None:
    """Negative 4: An unversioned database missing a required foreign key constraint fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_missing_fk_")
    try:
        db_path = Path(temp_dir) / "missing_fk.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Re-create messages table WITHOUT the foreign key constraint to conversations
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("DROP TABLE messages;"))
            conn.execute(
                text(
                    "CREATE TABLE messages ("
                    "id VARCHAR(64) PRIMARY KEY NOT NULL, "
                    "conversation_id VARCHAR(64) NOT NULL, "
                    "role VARCHAR(32) NOT NULL, "
                    "content TEXT NOT NULL, "
                    "data_classification VARCHAR(32) NOT NULL, "
                    "token_count INTEGER, "
                    "metadata_json JSON, "
                    "created_at DATETIME NOT NULL"
                    ");"
                )
            )
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="missing foreign key constraint") as exc_info:
            apply_pending_migrations(db_url)

        assert "conversations" in str(exc_info.value)
        assert "messages" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_unexpected_partially_upgraded_schema_fails_closed() -> None:
    """Negative 5: An unversioned database that already has post-baseline indexes fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_partial_upgrade_")
    try:
        db_path = Path(temp_dir) / "partial_upgrade.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Unexpected state: uq_perception_events_voice_session already present without alembic_version
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX uq_perception_events_voice_session "
                    "ON perception_events (session_id) "
                    "WHERE event_type = 'voice_transcript_delivery';"
                )
            )
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="Unexpected partially upgraded schema detected") as exc_info:
            apply_pending_migrations(db_url)

        assert "uq_perception_events_voice_session" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_voice_session_delivery_partial_unique_index_allows_unrelated_events() -> None:
    """Verify uq_perception_events_voice_session operates strictly on session_id and allows non-delivery events."""
    temp_dir = tempfile.mkdtemp(prefix="pico_index_id_")
    try:
        db_path = Path(temp_dir) / "index_id.db"
        db_url = f"sqlite:///{db_path.as_posix()}"

        apply_pending_migrations(db_url)
        engine = create_engine(db_url)
        Session = sessionmaker(bind=engine)
        shared_session_id = "aud_shared_test_123"

        with Session() as s:
            # 1. Multiple ambient noise samples with identical session_id are permitted
            ev1 = PerceptionEvent(
                id="ev_ambient_1",
                correlation_id="corr_1",
                modality="audio",
                event_type="ambient_noise_sample",
                source_device_id="mic:host",
                data_classification="INTERNAL",
                confidence=0.9,
                raw_payload_scrubbed=True,
                structured_data={"db": -40.0},
                provenance="sensor",
                session_id=shared_session_id,
            )
            ev2 = PerceptionEvent(
                id="ev_ambient_2",
                correlation_id="corr_2",
                modality="audio",
                event_type="ambient_noise_sample",
                source_device_id="mic:host",
                data_classification="INTERNAL",
                confidence=0.9,
                raw_payload_scrubbed=True,
                structured_data={"db": -41.0},
                provenance="sensor",
                session_id=shared_session_id,
            )
            s.add(ev1)
            s.add(ev2)
            s.commit()

        # 2. First delivery event with shared_session_id is permitted
        with Session() as s:
            ev3 = PerceptionEvent(
                id="ev_delivery_1",
                correlation_id="corr_3",
                modality="audio",
                event_type="voice_transcript_delivery",
                source_device_id="mic:host",
                data_classification="INTERNAL",
                confidence=1.0,
                raw_payload_scrubbed=True,
                structured_data={"transcript": "hello"},
                provenance="audio:mic:host",
                session_id=shared_session_id,
            )
            s.add(ev3)
            s.commit()

        # 3. Duplicate delivery event with the same session_id is REJECTED by the unique index
        with Session() as s:
            ev4 = PerceptionEvent(
                id="ev_delivery_2",
                correlation_id="corr_4",
                modality="audio",
                event_type="voice_transcript_delivery",
                source_device_id="mic:host",
                data_classification="INTERNAL",
                confidence=1.0,
                raw_payload_scrubbed=True,
                structured_data={"transcript": "duplicate hello"},
                provenance="audio:mic:host",
                session_id=shared_session_id,
            )
            s.add(ev4)
            with pytest.raises(Exception, match="UNIQUE constraint failed"):
                s.commit()

        engine.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_migration_file_lock_canonicalization_and_equivalence() -> None:
    """Verify _migration_file_lock path canonicalization and relative/absolute equivalence."""
    temp_dir = tempfile.mkdtemp(prefix="pico_lock_test_")
    try:
        db_path = Path(temp_dir) / "lock_target.db"
        abs_path_str = db_path.resolve().as_posix()
        url_rel = f"sqlite:///{db_path.name}"
        url_abs = f"sqlite:///{abs_path_str}"

        # Canonicalize should resolve relative and absolute paths
        can_rel = _canonicalize_sqlite_db_path(url_rel)
        can_abs = _canonicalize_sqlite_db_path(url_abs)
        assert can_abs is not None
        assert can_abs == db_path.resolve()
        assert can_rel == Path(db_path.name).resolve()

        # In-memory URLs return None (no lock file)
        assert _canonicalize_sqlite_db_path("sqlite:///:memory:") is None
        assert _canonicalize_sqlite_db_path("sqlite://") is None
        assert _canonicalize_sqlite_db_path("sqlite:///") is None

        # Lock acquisition on absolute URL creates .migration.lock beside target DB
        with _migration_file_lock(url_abs):
            expected_lock = db_path.parent / f"{db_path.name}.migration.lock"
            assert expected_lock.exists()

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_unrecognized_extra_column_fails_closed() -> None:
    """Negative: An unversioned database containing an unrecognized extra column fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_extra_col_")
    try:
        db_path = Path(temp_dir) / "extra_col.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Inject an unauthorized extra column into conversations
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE conversations ADD COLUMN rogue_extra_col TEXT;"))
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="unrecognized extra columns") as exc_info:
            apply_pending_migrations(db_url)

        assert "rogue_extra_col" in str(exc_info.value)
        assert "conversations" in str(exc_info.value)

        # Confirm database is untouched and not stamped
        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_missing_expected_index_fails_closed() -> None:
    """Negative: An unversioned database missing an expected baseline index fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_missing_idx_")
    try:
        db_path = Path(temp_dir) / "missing_idx.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Drop expected index on conversations
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("DROP INDEX ix_conversations_updated_at;"))
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="is missing expected index") as exc_info:
            apply_pending_migrations(db_url)

        assert "ix_conversations_updated_at" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def test_database_startup_legacy_index_column_mismatch_fails_closed() -> None:
    """Negative: An unversioned database with an index defined on the wrong column fails closed."""
    temp_dir = tempfile.mkdtemp(prefix="pico_bad_idx_")
    try:
        db_path = Path(temp_dir) / "bad_idx.db"
        db_url = f"sqlite:///{db_path.as_posix()}"
        _create_legacy_unversioned_db(db_url)

        # Invert/change columns indexed by ix_conversations_updated_at
        engine = create_engine(db_url)
        with engine.connect() as conn:
            conn.execute(text("DROP INDEX ix_conversations_updated_at;"))
            conn.execute(text("CREATE INDEX ix_conversations_updated_at ON conversations (title);"))
            conn.commit()
        engine.dispose()

        with pytest.raises(RuntimeError, match="column mismatch") as exc_info:
            apply_pending_migrations(db_url)

        assert "ix_conversations_updated_at" in str(exc_info.value)

        engine_check = create_engine(db_url)
        insp = inspect(engine_check)
        assert "alembic_version" not in insp.get_table_names()
        engine_check.dispose()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
