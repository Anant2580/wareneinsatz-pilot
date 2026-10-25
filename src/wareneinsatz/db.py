"""Database engine and sessions (SQLAlchemy 2)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from wareneinsatz.config import get_settings, normalise_database_url


def make_engine(url: str | None = None) -> Engine:
    """Create an engine. pool_pre_ping replaces connections that died, e.g. after a database restart."""
    return create_engine(normalise_database_url(url or get_settings().database_url), pool_pre_ping=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """One unit of work: everything inside the block is committed together, or rolled back on any error."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:  # begin() commits when the block ends normally, rolls back if it raises
        yield session
