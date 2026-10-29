"""Database tables (SQLAlchemy 2 ORM). The Alembic migrations in migrations/versions/ create them.

Invoice side (steps 1-3): suppliers, documents (uploaded files), invoices, their VAT
breakdown and invoice lines. Step 6: ingredients and the matching memory (supplier
products). Recipes and alerts follow in step 7 (see docs/schema.md).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Fixed names for constraints and indexes. Migrations stay predictable, and the repository
# can tell from the name which unique constraint was violated (e.g. "uq_documents_sha256").
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# Money is never stored as float: NUMERIC is exact, and Python reads it as Decimal.
MONEY = Numeric(14, 2)
# Unit prices may have many decimals (an official XRechnung test case uses 9), so they get 10.
UNIT_PRICE = Numeric(20, 10)
QUANTITY = Numeric(18, 4)
RATE = Numeric(5, 2)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Supplier(Base):
    """A wholesaler, cash & carry market or shop that sends invoices (EN 16931 BG-4 Seller)."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    # Normalised name ("metro deutschland gmbh"), used to recognise a supplier that has no VAT ID on the document.
    name_key: Mapped[str] = mapped_column(Text, unique=True)
    # USt-IdNr. NULLs never collide in a PostgreSQL unique constraint, so suppliers without one are fine.
    vat_id: Mapped[str | None] = mapped_column(Text, unique=True)
    tax_number: Mapped[str | None] = mapped_column(Text)
    street: Mapped[str | None] = mapped_column(Text)
    postcode: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(String(2), server_default="DE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    invoices: Mapped[list[Invoice]] = relationship(back_populates="supplier")


class Document(Base):
    """An uploaded file (XML, PDF or photo) and what the pipeline did with it."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("status IN ('received', 'extracted', 'failed')", name="status"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # SHA-256 of the file content: the same file uploaded twice is recognised and rejected.
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)  # application/xml, application/pdf, image/jpeg ...
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str | None] = mapped_column(Text)  # where the original file is kept (step 2)
    status: Mapped[str] = mapped_column(String(20), server_default="received")
    extraction_method: Mapped[str | None] = mapped_column(Text)  # e.g. "xrechnung-cii", "ocr+llm"
    extraction_ms: Mapped[int | None] = mapped_column(Integer)  # processing time per document, for the evaluation
    # The extractor's output before any human correction. Comparing it with the approved
    # invoice shows how accurate the extraction was.
    raw_extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # The invoice belongs to its document: deleting the document deletes the invoice and its lines.
    invoice: Mapped[Invoice | None] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )


class Invoice(Base):
    """The business content of a document: header and totals (EN 16931)."""

    __tablename__ = "invoices"
    __table_args__ = (
        # The same invoice can't be booked twice, even if it arrives as PDF and as XML.
        UniqueConstraint("supplier_id", "invoice_number"),
        CheckConstraint("document_type IN ('invoice', 'credit_note')", name="document_type"),
        CheckConstraint("status IN ('needs_review', 'approved', 'rejected')", name="status"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        # Price history needs a date, so an invoice can only be approved once its date is known.
        CheckConstraint(
            "status <> 'approved' OR (issue_date IS NOT NULL AND approved_at IS NOT NULL AND approved_by IS NOT NULL)",
            name="approved_has_date",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), unique=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), index=True)
    document_type: Mapped[str] = mapped_column(String(20), server_default="invoice")
    invoice_number: Mapped[str | None] = mapped_column(Text)
    issue_date: Mapped[date | None] = mapped_column(Date, index=True)
    delivery_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(String(3), server_default="EUR")
    prices_include_vat: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    allowance_total: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))
    charge_total: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))
    net_total: Mapped[Decimal | None] = mapped_column(MONEY)
    vat_total: Mapped[Decimal | None] = mapped_column(MONEY)
    gross_total: Mapped[Decimal] = mapped_column(MONEY)
    status: Mapped[str] = mapped_column(String(20), server_default="needs_review")
    # Findings of the validation step (step 3), shown on the review screen.
    review_issues: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 'auto' when the validation step approved it, otherwise the reviewer's name (review screen, step 8).
    approved_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="invoice")
    supplier: Mapped[Supplier] = relationship(back_populates="invoices")
    lines: Mapped[list[InvoiceLine]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True, order_by="InvoiceLine.line_no"
    )
    tax_totals: Mapped[list[InvoiceTaxTotal]] = relationship(
        back_populates="invoice",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="InvoiceTaxTotal.vat_rate",
    )


class InvoiceTaxTotal(Base):
    """VAT breakdown (EN 16931 BG-23): net amount and VAT per rate. Used to check the totals."""

    __tablename__ = "invoice_tax_totals"
    __table_args__ = (
        UniqueConstraint("invoice_id", "vat_rate"),
        CheckConstraint("vat_rate >= 0 AND vat_rate < 100", name="vat_rate_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    vat_rate: Mapped[Decimal] = mapped_column(RATE)
    taxable_amount: Mapped[Decimal] = mapped_column(MONEY)
    tax_amount: Mapped[Decimal] = mapped_column(MONEY)

    invoice: Mapped[Invoice] = relationship(back_populates="tax_totals")


class InvoiceLine(Base):
    """One invoice line (EN 16931 BG-25), stored exactly as printed on the document."""

    __tablename__ = "invoice_lines"
    __table_args__ = (
        UniqueConstraint("invoice_id", "line_no"),
        CheckConstraint("line_no >= 1", name="line_no_positive"),
        CheckConstraint("unit_price >= 0", name="unit_price_non_negative"),
        CheckConstraint("price_base_quantity > 0", name="price_base_quantity_positive"),
        CheckConstraint("vat_rate IS NULL OR (vat_rate >= 0 AND vat_rate < 100)", name="vat_rate_range"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"),
        CheckConstraint("gtin IS NULL OR gtin ~ '^([0-9]{8}|[0-9]{12,14})$'", name="gtin_format"),
        CheckConstraint(
            "match_status IS NULL OR match_status IN ('suggested', 'confirmed', 'no_match')", name="match_status"
        ),
        CheckConstraint(
            "match_method IS NULL OR match_method IN ('memory', 'fuzzy', 'embedding', 'hybrid', 'manual')",
            name="match_method",
        ),
        CheckConstraint("match_status <> 'confirmed' OR ingredient_id IS NOT NULL", name="confirmed_has_ingredient"),
        CheckConstraint("base_quantity IS NULL OR base_quantity <> 0", name="base_quantity_not_zero"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"))
    line_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    seller_article_no: Mapped[str | None] = mapped_column(Text)
    gtin: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QUANTITY)
    unit_code: Mapped[str | None] = mapped_column(Text)  # not a foreign key: unknown units are flagged, not rejected
    unit_raw: Mapped[str | None] = mapped_column(Text)
    unit_price: Mapped[Decimal] = mapped_column(UNIT_PRICE)
    price_base_quantity: Mapped[Decimal] = mapped_column(QUANTITY, server_default=text("1"))
    allowance_amount: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))  # line discounts
    charge_amount: Mapped[Decimal] = mapped_column(MONEY, server_default=text("0"))  # line surcharges
    line_amount: Mapped[Decimal] = mapped_column(MONEY)
    vat_rate: Mapped[Decimal | None] = mapped_column(RATE)
    confidence: Mapped[float | None] = mapped_column(Float)

    # Step 6: which ingredient this line is, how it was found, and how much of it (in g, ml or pieces)
    ingredient_id: Mapped[int | None] = mapped_column(ForeignKey("ingredients.id", ondelete="SET NULL"), index=True)
    supplier_product_id: Mapped[int | None] = mapped_column(ForeignKey("supplier_products.id", ondelete="SET NULL"))
    match_status: Mapped[str | None] = mapped_column(String(20))  # suggested, confirmed, no_match; NULL = not yet
    match_method: Mapped[str | None] = mapped_column(String(20))  # memory, fuzzy, embedding, hybrid, manual
    match_score: Mapped[float | None] = mapped_column(Float)
    base_quantity: Mapped[Decimal | None] = mapped_column(QUANTITY)  # quantity x pack size, in the base unit

    invoice: Mapped[Invoice] = relationship(back_populates="lines")
    ingredient: Mapped[Ingredient | None] = relationship()
    supplier_product: Mapped[SupplierProduct | None] = relationship()


class Ingredient(Base):
    """An ingredient on the restaurant's master list (data/ingredients.json), priced per base unit."""

    __tablename__ = "ingredients"
    __table_args__ = (CheckConstraint("base_unit IN ('g', 'ml', 'piece')", name="base_unit"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    key: Mapped[str] = mapped_column(Text, unique=True)  # stable id for recipes, e.g. "patty-rind-180"
    name: Mapped[str] = mapped_column(Text)
    base_unit: Mapped[str] = mapped_column(String(10))  # prices are compared per g, ml or piece
    category: Mapped[str | None] = mapped_column(Text)
    is_food: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))  # false: Pfand, napkins, ...
    aliases: Mapped[list[str]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SupplierProduct(Base):
    """The matching memory: a supplier's product, confirmed by a person to be a certain ingredient.

    Found again by article number or, if the supplier prints none, by the exact description.
    base_qty_per_unit says how much of the ingredient one invoice unit holds (a carton of
    "4 x 2,5 kg" = 10,000 g), read from the description or entered by the person.
    """

    __tablename__ = "supplier_products"
    __table_args__ = (
        Index(
            "uq_supplier_products_supplier_article",
            "supplier_id",
            "article_no",
            unique=True,
            postgresql_where=text("article_no IS NOT NULL"),
        ),
        Index(
            "uq_supplier_products_supplier_description",
            "supplier_id",
            "description_key",
            unique=True,
            postgresql_where=text("article_no IS NULL"),
        ),
        CheckConstraint("base_qty_per_unit IS NULL OR base_qty_per_unit > 0", name="base_qty_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"))
    article_no: Mapped[str | None] = mapped_column(Text)
    description_key: Mapped[str] = mapped_column(Text)  # description, case and spacing normalised
    description: Mapped[str] = mapped_column(Text)  # as printed when it was confirmed
    unit_code: Mapped[str | None] = mapped_column(Text)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id", ondelete="RESTRICT"))
    base_qty_per_unit: Mapped[Decimal | None] = mapped_column(QUANTITY)
    confirmed_by: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    supplier: Mapped[Supplier] = relationship()
    ingredient: Mapped[Ingredient] = relationship()
