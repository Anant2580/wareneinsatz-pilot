"""Shared test fixtures.

Database tests need TEST_DATABASE_URL (environment or .env) and are skipped
otherwise, so the pure unit tests run anywhere. CI provides a PostgreSQL 16.
"""

import io
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session

from wareneinsatz.config import get_settings
from wareneinsatz.schemas import InvoiceData

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
SAMPLES = Path(__file__).parent / "samples"


@pytest.fixture
def invoice_dict() -> dict[str, Any]:
    """A wholesaler invoice as raw JSON. Fictional supplier, realistic items for a burger restaurant."""
    return json.loads((FIXTURES / "invoice_musterfrisch.json").read_text(encoding="utf-8"))


@pytest.fixture
def invoice(invoice_dict: dict[str, Any]) -> InvoiceData:
    return InvoiceData.model_validate(invoice_dict)


@pytest.fixture
def samples() -> Path:
    """tests/samples: synthetic e-invoices (einvoices/) and official KoSIT test invoices (kosit/)."""
    return SAMPLES


@pytest.fixture
def make_pdf() -> Callable[..., bytes]:
    """Build a one-page PDF, optionally with attached files, e.g. a ZUGFeRD PDF with factur-x.xml inside."""

    def build(attachments: dict[str, bytes] | None = None) -> bytes:
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)  # A4 in points
        for name, data in (attachments or {}).items():
            writer.add_attachment(name, data)
        buffer = io.BytesIO()
        writer.write(buffer)
        return buffer.getvalue()

    return build


def alembic_config(connection: Connection) -> Config:
    """Alembic config that runs on the given connection instead of DATABASE_URL (see migrations/env.py)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = connection
    return config


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Migrate the test database up to the newest schema, and back down after all tests.

    Running the migrations in both directions on every test run proves they work.
    """
    url = get_settings().test_database_url
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set, skipping database tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    yield engine
    with engine.begin() as connection:
        command.downgrade(alembic_config(connection), "base")
    engine.dispose()


@pytest.fixture
def run_alembic(engine: Engine) -> Callable[..., None]:
    """Run an Alembic command (e.g. "check") against the test database."""

    def run(name: str, *args: str) -> None:
        with engine.begin() as connection:
            getattr(command, name)(alembic_config(connection), *args)

    return run


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose changes are rolled back after the test, so tests never see each other's data.

    The outer transaction is never committed; commits and savepoints inside the
    code under test become savepoints of it (join_transaction_mode).
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()
