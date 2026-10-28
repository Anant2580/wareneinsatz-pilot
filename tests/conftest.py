"""Shared test fixtures.

Database tests need TEST_DATABASE_URL (environment or .env) and are skipped
otherwise, so the pure unit tests run anywhere. CI provides a PostgreSQL 16.
"""

import io
import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from pypdf import PdfWriter
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session

from wareneinsatz.config import get_settings
from wareneinsatz.evaluation.render import de_date, de_money, de_number, de_price, printed_unit, row_amount
from wareneinsatz.evaluation.synthetic import Layout
from wareneinsatz.reading.llm import LlmReply
from wareneinsatz.reading.printed import PrintedInvoice, PrintedLine, PrintedSupplier, PrintedVatRate
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


# --- step 5: what a perfect reader would transcribe -----------------------------------------------------


def printed_from(data: InvoiceData, layout: Layout) -> PrintedInvoice:
    """The printed values of a rendered invoice, as a perfect model would copy them (German formats).

    Mirrors evaluation/render.py field by field, so interpreting it must give back the truth.
    """
    template = layout.template
    s = data.supplier
    codes = (
        {e.vat_rate: chr(ord("A") + i) for i, e in enumerate(data.vat_breakdown)} if template != "wholesaler" else {}
    )
    if template == "cash_carry":  # prints the VAT ID, or the tax number if there is none
        vat_id, tax_number = (f"USt-IdNr. {s.vat_id}", None) if s.vat_id else (None, f"St.-Nr. {s.tax_number}")
    else:
        vat_id = f"USt-IdNr.: {s.vat_id}" if s.vat_id else None
        tax_number = f"Steuernummer: {s.tax_number}" if s.tax_number else None
    lines = []
    for line in data.lines:
        lines.append(
            PrintedLine(
                article_no=line.seller_article_no if template != "receipt" else None,
                description=line.description[:34] if template == "receipt" else line.description,
                quantity=de_number(line.quantity),
                unit=printed_unit(line, layout) or None,
                unit_price=de_price(line.unit_price),
                amount=de_money(line.line_amount if template == "receipt" else row_amount(line)),
                discount=f"-{de_money(line.allowance_amount)}" if line.allowance_amount else None,
                surcharge=de_money(line.charge_amount) if line.charge_amount else None,
                vat=codes.get(line.vat_rate) if codes else f"{de_number(line.vat_rate or Decimal(0))} %",
            )
        )
    title = {"wholesaler": "Rechnung", "cash_carry": "RECHNUNG", "receipt": None}[template]
    return PrintedInvoice(
        document_title="Gutschrift" if data.document_type == "credit_note" else title,
        supplier=PrintedSupplier(
            name=s.name.upper() if template == "cash_carry" else s.name,
            address=f"{s.street}, {s.postcode} {s.city}",
            vat_id=vat_id,
            tax_number=tax_number,
        ),
        invoice_number=data.invoice_number if template != "receipt" else None,
        receipt_number=f"Bon {layout.receipt_number}" if template == "receipt" else None,
        issue_date=de_date(data.issue_date) or None,
        delivery_date=de_date(data.delivery_date) or None,
        currency="EUR",
        lines=lines,
        vat_rates=[
            PrintedVatRate(
                code=codes.get(e.vat_rate),
                rate=f"{de_number(e.vat_rate)} %",
                net=de_money(e.taxable_amount),
                tax=de_money(e.tax_amount),
            )
            for e in data.vat_breakdown
        ],
        discount_total=f"-{de_money(data.allowance_total)}" if data.allowance_total else None,
        surcharge_total=de_money(data.charge_total) if data.charge_total else None,
        net_total=de_money(data.net_total) if template != "receipt" and data.net_total is not None else None,
        vat_total=de_money(data.vat_total) if template == "cash_carry" and data.vat_total is not None else None,
        gross_total=de_money(data.gross_total),
    )


@pytest.fixture
def printed_of() -> Callable[[InvoiceData, Layout], PrintedInvoice]:
    return printed_from


class FakeLlm:
    """Stands in for the model server: returns a fixed reply and records every request."""

    def __init__(self, reply: str | Callable[[], str], *, model: str = "fake-vl:1b"):
        self.reply = reply
        self.model = model
        self.name = f"fake:{model}"
        self.requests: list[dict[str, Any]] = []

    def check(self) -> None:
        pass

    def complete(self, *, system: str, user: str, images: list[bytes], schema: dict[str, Any]) -> LlmReply:
        self.requests.append({"system": system, "user": user, "images": images, "schema": schema})
        content = self.reply() if callable(self.reply) else self.reply
        return LlmReply(content=content, model=self.model, seconds=0.01, prompt_tokens=900, output_tokens=300,
                        generation_seconds=0.01)  # fmt: skip


@pytest.fixture
def fake_llm() -> type[FakeLlm]:
    return FakeLlm
