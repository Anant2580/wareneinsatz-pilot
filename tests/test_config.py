"""Unit tests for settings and engine creation (no database needed)."""

import pytest

from wareneinsatz.config import Settings, normalise_database_url
from wareneinsatz.db import make_engine


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgresql://u:p@localhost:5433/db", "postgresql+psycopg://u:p@localhost:5433/db"),
        ("postgres://u:p@localhost/db", "postgresql+psycopg://u:p@localhost/db"),
        ("postgresql+psycopg://u:p@localhost/db", "postgresql+psycopg://u:p@localhost/db"),
    ],
)
def test_urls_always_use_the_psycopg_driver(url, expected):
    assert normalise_database_url(url) == expected


def test_settings_come_from_environment_variables(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:secret@db:5432/wareneinsatz")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)  # ignore a local .env file in this test
    assert settings.database_url == "postgresql+psycopg://app:secret@db:5432/wareneinsatz"
    assert settings.test_database_url is None


def test_make_engine_does_not_connect_yet():
    """Creating an engine is cheap: it only connects on first use, so this works without a database."""
    engine = make_engine("postgres://u:p@localhost:1/db")
    assert engine.url.drivername == "postgresql+psycopg"
    engine.dispose()
