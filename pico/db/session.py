import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
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


def _canonicalize_sqlite_db_path(db_url: str) -> Path | None:
    """Extract and canonicalize physical filesystem path from a SQLite database URL."""
    if not db_url.startswith("sqlite:"):
        return None
    if ":memory:" in db_url or db_url in ("sqlite://", "sqlite:///"):
        return None
    prefix = "sqlite:///" if db_url.startswith("sqlite:///") else "sqlite://"
    raw_path = db_url[len(prefix) :]
    if "?" in raw_path:
        raw_path = raw_path.split("?", 1)[0]
    if not raw_path or raw_path == ":memory:":
        return None
    return Path(raw_path).resolve()


@contextmanager
def _migration_file_lock(db_url: str) -> Generator[None, None, None]:
    """Cross-process and cross-thread lock for deterministic database migration execution."""
    import sys
    import time

    with _MIGRATION_THREAD_LOCK:
        db_file = _canonicalize_sqlite_db_path(db_url)
        if db_file is not None:
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


def validate_legacy_unversioned_schema(insp: Any, db_url: str) -> None:
    """Validate that an unversioned SQLite database strictly matches the recognized legacy baseline schema.

    Enforces all 18 historical tables for:
    - Required table presence
    - Required column presence, type compatibility, and nullability
    - Primary key constraints
    - Foreign key constraints
    - Rejection of unexpected partially upgraded states (e.g. index already exists without alembic_version)
    """
    from pico.db.legacy_baseline_manifest import LEGACY_BASELINE_SCHEMA

    table_names = set(insp.get_table_names())
    expected_tables = set(LEGACY_BASELINE_SCHEMA.keys())

    # 1. Check all 18 required baseline tables
    if not expected_tables.issubset(table_names):
        missing = sorted(expected_tables - table_names)
        diagnostic = (
            f"Ambiguous unversioned database schema detected on {db_url}. "
            f"Found tables: {sorted(table_names)}. "
            f"Missing expected baseline tables: {missing}. "
            f"Refusing to guess or stamp an arbitrary migration revision."
        )
        raise RuntimeError(diagnostic)

    # 2. Check each table's columns, types, nullability, PKs, FKs
    for table_name, expected_spec in LEGACY_BASELINE_SCHEMA.items():
        actual_cols = {c["name"]: c for c in insp.get_columns(table_name)}
        expected_cols = expected_spec["columns"]

        # Missing column
        missing_cols = sorted(set(expected_cols.keys()) - set(actual_cols.keys()))
        if missing_cols:
            raise RuntimeError(
                f"Incompatible unversioned database schema on {db_url}: table '{table_name}' "
                f"is missing required columns {missing_cols}."
            )

        # Unrecognized extra columns
        extra_cols = sorted(set(actual_cols.keys()) - set(expected_cols.keys()))
        if extra_cols:
            raise RuntimeError(
                f"Incompatible unversioned database schema on {db_url}: table '{table_name}' "
                f"contains unrecognized extra columns {extra_cols}."
            )

        # Nullability & Type compatibility
        for col_name, exp_col in expected_cols.items():
            act_col = actual_cols[col_name]
            if act_col["nullable"] != exp_col["nullable"]:
                raise RuntimeError(
                    f"Incompatible unversioned database schema on {db_url}: column '{table_name}.{col_name}' "
                    f"nullability mismatch (got nullable={act_col['nullable']}, expected {exp_col['nullable']})."
                )

            act_type = str(act_col["type"]).upper().split("(")[0].strip()
            exp_type = exp_col["type"].split("(")[0].strip()
            if act_type != exp_type:
                raise RuntimeError(
                    f"Incompatible unversioned database schema on {db_url}: column '{table_name}.{col_name}' "
                    f"type mismatch (got {act_type}, expected {exp_type})."
                )

        # Primary Key
        act_pk = insp.get_pk_constraint(table_name).get("constrained_columns") or []
        if sorted(act_pk) != sorted(expected_spec["primary_key"]):
            raise RuntimeError(
                f"Incompatible unversioned database schema on {db_url}: table '{table_name}' "
                f"primary key mismatch (got {act_pk}, expected {expected_spec['primary_key']})."
            )

        # Foreign Keys
        act_fks = insp.get_foreign_keys(table_name)
        for exp_fk in expected_spec["foreign_keys"]:
            matched = any(
                fk.get("referred_table") == exp_fk["referred_table"]
                and sorted(fk.get("constrained_columns") or []) == sorted(exp_fk["constrained_columns"])
                and sorted(fk.get("referred_columns") or []) == sorted(exp_fk["referred_columns"])
                for fk in act_fks
            )
            if not matched:
                raise RuntimeError(
                    f"Incompatible unversioned database schema on {db_url}: table '{table_name}' "
                    f"missing foreign key constraint on {exp_fk['constrained_columns']} -> {exp_fk['referred_table']}."
                )

        # Expected Indexes
        act_indexes = {
            idx["name"]: idx for idx in insp.get_indexes(table_name) if not idx["name"].startswith("sqlite_autoindex_")
        }
        for exp_idx in expected_spec.get("indexes", []):
            idx_name = exp_idx["name"]
            if idx_name not in act_indexes:
                raise RuntimeError(
                    f"Incompatible unversioned database schema on {db_url}: table '{table_name}' "
                    f"is missing expected index '{idx_name}'."
                )
            actual_idx = act_indexes[idx_name]
            if actual_idx.get("column_names") != exp_idx["column_names"]:
                raise RuntimeError(
                    f"Incompatible unversioned database schema on {db_url}: index '{idx_name}' on table '{table_name}' "
                    f"column mismatch (got {actual_idx.get('column_names')}, expected {exp_idx['column_names']})."
                )
            if idx_name != "ix_workflow_runs_idempotency_key":
                if bool(actual_idx.get("unique")) != bool(exp_idx["unique"]):
                    raise RuntimeError(
                        f"Incompatible unversioned schema on {db_url}: index '{idx_name}' on table '{table_name}' "
                        f"unique mismatch (got {actual_idx.get('unique')}, expected {exp_idx['unique']})."
                    )

    # 3. Check for unexpected partially upgraded schema on perception_events
    pe_indexes = [idx["name"] for idx in insp.get_indexes("perception_events")]
    if "uq_perception_events_voice_session" in pe_indexes:
        raise RuntimeError(
            f"Unexpected partially upgraded schema detected on {db_url}: "
            f"table 'perception_events' already contains index 'uq_perception_events_voice_session', "
            f"but 'alembic_version' is absent. Database may be partially migrated or corrupt."
        )


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
                # Rigorously validate all 18 tables against the recognized P11.2 unversioned baseline
                validate_legacy_unversioned_schema(insp, db_url)
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
