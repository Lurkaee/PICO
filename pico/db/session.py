import threading
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pico.core.config import settings

connect_args: dict[str, Any] = {}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args["check_same_thread"] = False

engine: Engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def configure_sqlite_connection(dbapi_connection: Any, connection_record: Any) -> None:
    """Enforce SQLite WAL mode and immediate foreign key constraints."""
    if settings.DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON;")
        cursor.execute("PRAGMA journal_mode = WAL;")
        cursor.close()


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a managed transactional database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


LEGACY_UNVERSIONED_TABLES: frozenset[str] = frozenset(
    {
        "conversations",
        "episodic_memory_items",
        "event_ledger",
        "messages",
        "operator_pairing_challenges",
        "operator_sessions",
        "perception_events",
        "procedural_memories",
        "projects",
        "routing_decisions",
        "semantic_memories",
        "tasks",
        "tool_executions",
        "workflow_compensations",
        "workflow_runs",
        "workflow_steps",
        "workflow_verifications",
        "working_memory_items",
    }
)

_MIGRATION_THREAD_LOCK = threading.Lock()


@contextmanager
def _migration_file_lock(db_url: str) -> Generator[None, None, None]:
    """Cross-process and cross-thread lock for deterministic database migration execution."""
    import sys
    import time
    from pathlib import Path

    with _MIGRATION_THREAD_LOCK:
        if db_url.startswith("sqlite:///") and ":memory:" not in db_url:
            raw_path = db_url[len("sqlite:///") :]
            if "?" in raw_path:
                raw_path = raw_path.split("?", 1)[0]
            db_file = Path(raw_path)
            lock_path = db_file.parent / f"{db_file.name}.migration.lock"
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+b") as lock_file:
                deadline = time.time() + 30.0
                locked = False
                while time.time() < deadline:
                    try:
                        if sys.platform == "win32":
                            import msvcrt

                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        locked = True
                        break
                    except (OSError, BlockingIOError):
                        time.sleep(0.05)
                if not locked:
                    raise TimeoutError(f"Timed out after 30s waiting for migration lock on {lock_path}")
                try:
                    yield
                finally:
                    try:
                        if sys.platform == "win32":
                            import msvcrt

                            lock_file.seek(0)
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        else:
            yield


def apply_pending_migrations(target_database_url: str | None = None) -> None:
    """Apply pending Alembic migrations up to head.

    Ensures that real PICO Core startup deterministically executes schema migrations
    (including partial indexes and table upgrades) rather than relying solely on create_all().
    Locates alembic.ini relative to this module or the repository root.
    Validates legacy unversioned schemas against recognized baseline specifications
    and fails closed with actionable diagnostics if schema compatibility is ambiguous.
    """
    import logging
    from pathlib import Path

    from alembic.config import Config
    from sqlalchemy import inspect

    from alembic import command

    logger = logging.getLogger(__name__)

    # Search for alembic.ini in known locations:
    # 1. Current working directory
    # 2. Parent of pico package (repository root or staged core root)
    candidates = [
        Path.cwd() / "alembic.ini",
        Path(__file__).resolve().parent.parent.parent / "alembic.ini",
    ]
    ini_path: Path | None = None
    for cand in candidates:
        if cand.is_file():
            ini_path = cand
            break

    db_url = target_database_url or settings.DATABASE_URL

    if not ini_path:
        logger.warning("alembic.ini not found in candidates %s; falling back to Base.metadata.create_all.", candidates)
        from pico.db.base import Base

        check_engine = engine if str(engine.url) == db_url else create_engine(db_url)
        try:
            Base.metadata.create_all(bind=check_engine)
        finally:
            if check_engine is not engine:
                check_engine.dispose()
        return

    alembic_cfg = Config(str(ini_path))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    # Ensure script_location is resolved relative to the directory containing alembic.ini
    script_loc = ini_path.parent / "alembic"
    if script_loc.is_dir():
        alembic_cfg.set_main_option("script_location", str(script_loc))

    with _migration_file_lock(db_url):
        check_engine = engine if str(engine.url) == db_url else create_engine(db_url)
        try:
            insp = inspect(check_engine)
            table_names = set(insp.get_table_names())

            # 1. If database is already tracked by Alembic, inspect current revision
            if "alembic_version" in table_names:
                with check_engine.connect() as conn:
                    current_rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
                if current_rev == "c3d4e5f6a7b8":
                    logger.info("Database on %s already migrated to head revision %s.", db_url, current_rev)
                    return

            # 2. Legacy unversioned database: tables exist, but alembic_version table is absent
            elif table_names:
                # Rigorously validate that existing schema matches the recognized P11.2 unversioned baseline
                if not LEGACY_UNVERSIONED_TABLES.issubset(table_names):
                    missing_tables = sorted(LEGACY_UNVERSIONED_TABLES - table_names)
                    diagnostic = (
                        f"Ambiguous unversioned database schema detected on {db_url}. "
                        f"Found tables: {sorted(table_names)}. "
                        f"Missing expected baseline tables: {missing_tables}. "
                        f"Refusing to guess or stamp an arbitrary migration revision. "
                        f"Manual inspection or database recovery is required."
                    )
                    logger.error(diagnostic)
                    raise RuntimeError(diagnostic)

                # Validate key baseline table column definitions
                pe_cols = {c["name"] for c in insp.get_columns("perception_events")}
                if not {"id", "session_id", "modality", "event_type", "data_classification"}.issubset(pe_cols):
                    diagnostic = (
                        f"Ambiguous unversioned perception_events schema on {db_url}. "
                        f"Columns {pe_cols} do not match expected baseline."
                    )
                    logger.error(diagnostic)
                    raise RuntimeError(diagnostic)

                # Check if voice session unique index is already present
                indexes = [idx["name"] for idx in insp.get_indexes("perception_events")]
                if "uq_perception_events_voice_session" in indexes:
                    logger.info(
                        "Recognized legacy unversioned schema with voice index already present on %s; stamping head.",
                        db_url,
                    )
                    command.stamp(alembic_cfg, "c3d4e5f6a7b8")
                    return

                logger.info(
                    "Recognized verified legacy unversioned P11.2 schema baseline on %s; stamping b2c3d4e5f6a7.",
                    db_url,
                )
                command.stamp(alembic_cfg, "b2c3d4e5f6a7")

        finally:
            if check_engine is not engine:
                check_engine.dispose()

        logger.info("Executing pending Alembic migrations on %s using %s ...", db_url, ini_path)
        command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations successfully applied to head.")


def check_db_health(target_engine: Engine | None = None) -> dict[str, Any]:
    """Inspect and return current database connection health and pragmas."""
    active_engine = target_engine or engine
    with active_engine.connect() as conn:
        connected = bool(conn.execute(text("SELECT 1")).scalar() == 1)
        if active_engine.url.drivername.startswith("sqlite"):
            foreign_keys = bool(conn.execute(text("PRAGMA foreign_keys")).scalar() == 1)
            journal_mode = str(conn.execute(text("PRAGMA journal_mode")).scalar()).lower()
        else:
            foreign_keys = True
            journal_mode = "postgres"

    return {
        "connected": connected,
        "foreign_keys": foreign_keys,
        "journal_mode": journal_mode,
    }
