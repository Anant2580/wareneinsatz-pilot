"""Invoice core tables: suppliers, documents, invoices, invoice_tax_totals, invoice_lines.

Stores supplier invoices exactly as extracted (EN 16931 structure). Matching to
ingredients, recipes and alerts are added by later migrations.

Revision ID: 0001
Revises:
Create Date: 2026-10-24 21:19:39.313132
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Uploaded files; the SHA-256 hash makes re-uploads of the same file detectable.
    op.create_table(
        "documents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="received", nullable=False),
        sa.Column("extraction_method", sa.Text(), nullable=True),
        sa.Column("extraction_ms", sa.Integer(), nullable=True),
        sa.Column("raw_extraction", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_documents_sha256_hex")),
        sa.CheckConstraint("status IN ('received', 'extracted', 'failed')", name=op.f("ck_documents_status")),
        sa.CheckConstraint("size_bytes >= 0", name=op.f("ck_documents_size_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("sha256", name=op.f("uq_documents_sha256")),
    )
    # Suppliers are recognised by VAT ID, or by normalised name when a document has no VAT ID.
    op.create_table(
        "suppliers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("vat_id", sa.Text(), nullable=True),
        sa.Column("tax_number", sa.Text(), nullable=True),
        sa.Column("street", sa.Text(), nullable=True),
        sa.Column("postcode", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("country_code", sa.String(length=2), server_default="DE", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suppliers")),
        sa.UniqueConstraint("name_key", name=op.f("uq_suppliers_name_key")),
        sa.UniqueConstraint("vat_id", name=op.f("uq_suppliers_vat_id")),
    )
    # One invoice per document; (supplier, invoice number) is unique, so nothing is booked twice.
    op.create_table(
        "invoices",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("document_id", sa.BigInteger(), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("document_type", sa.String(length=20), server_default="invoice", nullable=False),
        sa.Column("invoice_number", sa.Text(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("delivery_date", sa.Date(), nullable=True),
        sa.Column("currency", sa.String(length=3), server_default="EUR", nullable=False),
        sa.Column("prices_include_vat", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("allowance_total", sa.Numeric(precision=14, scale=2), server_default=sa.text("0"), nullable=False),
        sa.Column("charge_total", sa.Numeric(precision=14, scale=2), server_default=sa.text("0"), nullable=False),
        sa.Column("net_total", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("vat_total", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("gross_total", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="needs_review", nullable=False),
        sa.Column(
            "review_issues",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'", name=op.f("ck_invoices_currency_iso")),
        sa.CheckConstraint("document_type IN ('invoice', 'credit_note')", name=op.f("ck_invoices_document_type")),
        sa.CheckConstraint(
            "status <> 'approved' OR (issue_date IS NOT NULL AND approved_at IS NOT NULL)",
            name=op.f("ck_invoices_approved_has_date"),
        ),
        sa.CheckConstraint("status IN ('needs_review', 'approved', 'rejected')", name=op.f("ck_invoices_status")),
        sa.CheckConstraint(
            "allowance_total >= 0 AND charge_total >= 0", name=op.f("ck_invoices_allowance_charge_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.id"], name=op.f("fk_invoices_document_id_documents"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"], ["suppliers.id"], name=op.f("fk_invoices_supplier_id_suppliers"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
        sa.UniqueConstraint("document_id", name=op.f("uq_invoices_document_id")),
        sa.UniqueConstraint("supplier_id", "invoice_number", name=op.f("uq_invoices_supplier_id_invoice_number")),
    )
    op.create_index(op.f("ix_invoices_issue_date"), "invoices", ["issue_date"], unique=False)
    op.create_index(op.f("ix_invoices_supplier_id"), "invoices", ["supplier_id"], unique=False)
    # Invoice lines as printed: description, quantity, unit, price, VAT rate.
    op.create_table(
        "invoice_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("seller_article_no", sa.Text(), nullable=True),
        sa.Column("gtin", sa.Text(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit_code", sa.Text(), nullable=True),
        sa.Column("unit_raw", sa.Text(), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "price_base_quantity", sa.Numeric(precision=18, scale=4), server_default=sa.text("1"), nullable=False
        ),
        sa.Column("line_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("vat_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "gtin IS NULL OR gtin ~ '^([0-9]{8}|[0-9]{12,14})$'", name=op.f("ck_invoice_lines_gtin_format")
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name=op.f("ck_invoice_lines_confidence_range"),
        ),
        sa.CheckConstraint("line_no >= 1", name=op.f("ck_invoice_lines_line_no_positive")),
        sa.CheckConstraint("price_base_quantity > 0", name=op.f("ck_invoice_lines_price_base_quantity_positive")),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_invoice_lines_unit_price_non_negative")),
        sa.CheckConstraint(
            "vat_rate IS NULL OR (vat_rate >= 0 AND vat_rate < 100)", name=op.f("ck_invoice_lines_vat_rate_range")
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name=op.f("fk_invoice_lines_invoice_id_invoices"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_lines")),
        sa.UniqueConstraint("invoice_id", "line_no", name=op.f("uq_invoice_lines_invoice_id_line_no")),
    )
    # VAT breakdown per rate, used to check that the totals add up.
    op.create_table(
        "invoice_tax_totals",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("invoice_id", sa.BigInteger(), nullable=False),
        sa.Column("vat_rate", sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column("taxable_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.CheckConstraint("vat_rate >= 0 AND vat_rate < 100", name=op.f("ck_invoice_tax_totals_vat_rate_range")),
        sa.ForeignKeyConstraint(
            ["invoice_id"], ["invoices.id"], name=op.f("fk_invoice_tax_totals_invoice_id_invoices"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoice_tax_totals")),
        sa.UniqueConstraint("invoice_id", "vat_rate", name=op.f("uq_invoice_tax_totals_invoice_id_vat_rate")),
    )


def downgrade() -> None:
    # Drop in reverse order of the foreign keys.
    op.drop_table("invoice_tax_totals")
    op.drop_table("invoice_lines")
    op.drop_index(op.f("ix_invoices_supplier_id"), table_name="invoices")
    op.drop_index(op.f("ix_invoices_issue_date"), table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("suppliers")
    op.drop_table("documents")
