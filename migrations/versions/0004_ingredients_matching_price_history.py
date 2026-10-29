"""Ingredients, the matching memory and the price history view (step 6).

- ingredients: the restaurant's master list; prices are compared per base unit (g, ml, piece).
- supplier_products: a supplier's product confirmed by a person as an ingredient, found again by
  article number or exact description, with the amount of ingredient per invoice unit.
- invoice_lines: which ingredient a line is (suggested or confirmed), how it was matched, and
  its quantity in the ingredient's base unit.
- v_ingredient_prices: net price per kg / l / piece for every confirmed line of an approved
  invoice. Gross receipt prices are converted to net here, with the line's VAT rate.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29 17:17:42
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


LINE_CHECKS = {
    "ck_invoice_lines_match_status": "match_status IS NULL OR match_status IN ('suggested', 'confirmed', 'no_match')",
    "ck_invoice_lines_match_method": (
        "match_method IS NULL OR match_method IN ('memory', 'fuzzy', 'embedding', 'hybrid', 'manual')"
    ),
    "ck_invoice_lines_confirmed_has_ingredient": "match_status <> 'confirmed' OR ingredient_id IS NOT NULL",
    "ck_invoice_lines_base_quantity_not_zero": "base_quantity IS NULL OR base_quantity <> 0",
}

# Net price per base unit of every confirmed line of an approved invoice (credit notes and
# returns excluded). Till receipts print gross prices: net = gross / (1 + VAT rate).
PRICE_VIEW = """
CREATE VIEW v_ingredient_prices AS
SELECT
    l.id AS invoice_line_id,
    i.id AS invoice_id,
    i.issue_date,
    s.id AS supplier_id,
    s.name AS supplier_name,
    g.id AS ingredient_id,
    g.key AS ingredient_key,
    g.name AS ingredient_name,
    g.base_unit,
    g.is_food,
    l.description,
    l.base_quantity,
    n.net_amount,
    n.net_amount / l.base_quantity AS net_price_per_base_unit,
    n.net_amount / l.base_quantity * CASE g.base_unit WHEN 'piece' THEN 1 ELSE 1000 END AS net_price_per_display_unit,
    CASE g.base_unit WHEN 'g' THEN 'kg' WHEN 'ml' THEN 'l' ELSE 'piece' END AS display_unit
FROM invoice_lines l
JOIN invoices i ON i.id = l.invoice_id
JOIN suppliers s ON s.id = i.supplier_id
JOIN ingredients g ON g.id = l.ingredient_id
CROSS JOIN LATERAL (
    SELECT CASE WHEN i.prices_include_vat
                THEN l.line_amount / (1 + COALESCE(l.vat_rate, 0) / 100)
                ELSE l.line_amount END AS net_amount
) n
WHERE i.status = 'approved'
  AND i.document_type = 'invoice'
  AND l.match_status = 'confirmed'
  AND l.base_quantity > 0
  AND l.line_amount > 0
"""


def upgrade() -> None:
    op.create_table(
        "ingredients",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_unit", sa.String(length=10), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("is_food", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "aliases", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("base_unit IN ('g', 'ml', 'piece')", name=op.f("ck_ingredients_base_unit")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingredients")),
        sa.UniqueConstraint("key", name=op.f("uq_ingredients_key")),
    )
    op.create_table(
        "supplier_products",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False),
        sa.Column("article_no", sa.Text(), nullable=True),
        sa.Column("description_key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("unit_code", sa.Text(), nullable=True),
        sa.Column("ingredient_id", sa.BigInteger(), nullable=False),
        sa.Column("base_qty_per_unit", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("confirmed_by", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "base_qty_per_unit IS NULL OR base_qty_per_unit > 0", name=op.f("ck_supplier_products_base_qty_positive")
        ),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredients.id"],
            name=op.f("fk_supplier_products_ingredient_id_ingredients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
            name=op.f("fk_supplier_products_supplier_id_suppliers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_supplier_products")),
    )
    op.create_index(
        "uq_supplier_products_supplier_article",
        "supplier_products",
        ["supplier_id", "article_no"],
        unique=True,
        postgresql_where=sa.text("article_no IS NOT NULL"),
    )
    op.create_index(
        "uq_supplier_products_supplier_description",
        "supplier_products",
        ["supplier_id", "description_key"],
        unique=True,
        postgresql_where=sa.text("article_no IS NULL"),
    )
    op.add_column("invoice_lines", sa.Column("ingredient_id", sa.BigInteger(), nullable=True))
    op.add_column("invoice_lines", sa.Column("supplier_product_id", sa.BigInteger(), nullable=True))
    op.add_column("invoice_lines", sa.Column("match_status", sa.String(length=20), nullable=True))
    op.add_column("invoice_lines", sa.Column("match_method", sa.String(length=20), nullable=True))
    op.add_column("invoice_lines", sa.Column("match_score", sa.Float(), nullable=True))
    op.add_column("invoice_lines", sa.Column("base_quantity", sa.Numeric(precision=18, scale=4), nullable=True))
    op.create_foreign_key(
        op.f("fk_invoice_lines_supplier_product_id_supplier_products"),
        "invoice_lines",
        "supplier_products",
        ["supplier_product_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_invoice_lines_ingredient_id_ingredients"),
        "invoice_lines",
        "ingredients",
        ["ingredient_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(op.f("ix_invoice_lines_ingredient_id"), "invoice_lines", ["ingredient_id"])
    for name, condition in LINE_CHECKS.items():
        # op.f(): the name is final, the naming convention must not add its prefix again
        op.create_check_constraint(op.f(name), "invoice_lines", condition)
    op.execute(PRICE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW v_ingredient_prices")
    for name in LINE_CHECKS:
        op.drop_constraint(op.f(name), "invoice_lines", type_="check")
    op.drop_index(op.f("ix_invoice_lines_ingredient_id"), table_name="invoice_lines")
    op.drop_constraint(op.f("fk_invoice_lines_ingredient_id_ingredients"), "invoice_lines", type_="foreignkey")
    op.drop_constraint(
        op.f("fk_invoice_lines_supplier_product_id_supplier_products"), "invoice_lines", type_="foreignkey"
    )
    op.drop_column("invoice_lines", "base_quantity")
    op.drop_column("invoice_lines", "match_score")
    op.drop_column("invoice_lines", "match_method")
    op.drop_column("invoice_lines", "match_status")
    op.drop_column("invoice_lines", "supplier_product_id")
    op.drop_column("invoice_lines", "ingredient_id")
    op.drop_index(
        "uq_supplier_products_supplier_description",
        table_name="supplier_products",
        postgresql_where=sa.text("article_no IS NULL"),
    )
    op.drop_index(
        "uq_supplier_products_supplier_article",
        table_name="supplier_products",
        postgresql_where=sa.text("article_no IS NOT NULL"),
    )
    op.drop_table("supplier_products")
    op.drop_table("ingredients")
