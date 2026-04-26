from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# Read-write engine — used by ingestion pipeline
_rw_engine = create_engine(settings.database_url, pool_pre_ping=True)
RWSession = sessionmaker(bind=_rw_engine, autoflush=False, autocommit=False)

# Read-only engine — used by chat backend
_ro_engine = create_engine(settings.readonly_database_url, pool_pre_ping=True)
ROSession = sessionmaker(bind=_ro_engine, autoflush=False, autocommit=False)


def get_rw_session() -> Generator[Session, None, None]:
    session = RWSession()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_ro_session() -> Generator[Session, None, None]:
    session = ROSession()
    try:
        yield session
    finally:
        session.close()
