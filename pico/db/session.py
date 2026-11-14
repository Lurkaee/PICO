from collections.abc import Generator
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


def apply_pending_migrations(target_database_url: str | None = None) -> None:
    """Apply pending Alembic migrations up to head.

    Ensures that real PICO Core startup deterministically executes schema migrations
    (including partial indexes and table upgrades) rather than relying solely on create_all().
    Locates alembic.ini relative to this module or the repository root.
    """
    import logging
    from pathlib import Path

    from alembic.config import Config

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

        check_engine = (
            engine
            if (not target_database_url or target_database_url == settings.DATABASE_URL)
            else create_engine(db_url)
        )
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

    # Check whether the database already has tables created without alembic_version (e.g. from create_all)
    from sqlalchemy import inspect

    check_engine = (
        engine
        if (not target_database_url or target_database_url == settings.DATABASE_URL)
        else create_engine(db_url)
    )
    try:
        insp = inspect(check_engine)
        table_names = set(insp.get_table_names())
        if table_names and "alembic_version" not in table_names:
            # Tables exist without Alembic tracking. Stamp with appropriate revision so upgrade can proceed.
            if "operator_sessions" in table_names:
                logger.info("Database has operator_sessions but no alembic_version; stamping b2c3d4e5f6a7.")
                command.stamp(alembic_cfg, "b2c3d4e5f6a7")
            elif "conversations" in table_names:
                logger.info("Database has conversations but no alembic_version; stamping 3c659cf0ae57.")
                command.stamp(alembic_cfg, "3c659cf0ae57")
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
