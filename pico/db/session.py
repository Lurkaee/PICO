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
