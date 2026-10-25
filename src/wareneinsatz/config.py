"""Application settings, read from environment variables or a local .env file.

All configuration lives in one place, so no other module reads os.environ directly.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalise_database_url(url: str) -> str:
    """Make sure SQLAlchemy uses the psycopg 3 driver.

    'postgresql://user:pw@host/db' would make SQLAlchemy look for the older psycopg2 driver,
    which is not installed, so it becomes 'postgresql+psycopg://user:pw@host/db'.
    """
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


class Settings(BaseSettings):
    """Typed settings. Variable names are case-insensitive: DATABASE_URL fills database_url."""

    # .env is optional: in Docker and in CI the values come from real environment variables.
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Default = the local database from docker-compose.yml (host port 5433).
    database_url: str = "postgresql+psycopg://wareneinsatz:wareneinsatz@localhost:5433/wareneinsatz"
    # Separate database for the tests; database tests are skipped when it is not set.
    test_database_url: str | None = None
    # Where original files are kept (relative to the folder the app runs in). Never committed to git.
    document_dir: Path = Path("data/documents")

    @field_validator("database_url", "test_database_url")
    @classmethod
    def use_psycopg_driver(cls, value: str | None) -> str | None:
        return normalise_database_url(value) if value else value


@lru_cache
def get_settings() -> Settings:
    """Read the settings once and reuse them."""
    return Settings()
