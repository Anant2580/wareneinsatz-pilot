"""Alembic environment: how migrations connect to the database.

Normally the URL comes from DATABASE_URL (environment or .env). The tests pass in
an open connection instead (config.attributes["connection"]), so they can migrate
the test database inside their own transaction.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, create_engine, pool

from wareneinsatz.config import get_settings
from wareneinsatz.models import Base

config = context.config

# Set up logging from alembic.ini, but keep loggers that already exist (e.g. pytest's).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# The tables as defined in models.py: autogenerate and "alembic check" compare the database against this.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Print the SQL instead of running it: uv run alembic upgrade head --sql"""
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against a live database."""
    connection = config.attributes.get("connection")
    if connection is not None:  # passed in by the tests
        _run(connection)
        return

    # NullPool: a migration run is short-lived, so no connection pool is needed.
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        _run(connection)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
