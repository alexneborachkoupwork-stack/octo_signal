"""
SQLAlchemy engine/session factory — SQLite, WAL mode.

Single writer (the Hub, in later phases), single machine. WAL mode lets concurrent
readers (dashboard queries) not block on the writer.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_DB_PATH = Path(__file__).parent / "octo.sqlite3"
_DB_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(_DB_URL, echo=False, future=True)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_session() -> Session:
    """Caller is responsible for closing (use as a context manager or call .close())."""
    return SessionLocal()
