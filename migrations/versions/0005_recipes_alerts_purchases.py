"""Recipes, alerts and the purchase lines view (step 7).

- recipes: dishes (menu price, VAT rate, food-cost target) and preparations (with a yield).
- recipe_items: an ingredient or a preparation per recipe, exactly one of the two, with
  quantity, unit and waste percentage.
- alerts: price jumps, cheaper suppliers, dishes above target; dedupe_key keeps each finding once.
- v_purchase_lines: every line of an approved invoice with its net amount (gross receipts
  converted, credit notes negative) and month, matched ingredient and category if confirmed.
  The monthly Wareneinsatz is the sum of the food lines.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-30 13:40:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PURCHASE_VIEW = """
CREATE VIEW v_purchase_lines AS
SELECT
    l.id AS invoice_line_id,
    i.id AS invoice_id,
    i.issue_date,
    date_trunc('month', i.issue_date)::date AS month,
    s.name AS supplier_name,
    l.description,
    l.match_status,
    g.key AS ingredient_key,
    g.category,
    g.is_food,
    n.net_amount * CASE WHEN i.document_type = 'credit_note' THEN -1 ELSE 1 END AS net_amount
FROM invoice_lines l
JOIN invoices i ON i.id = l.invoice_id
JOIN suppliers s ON s.id = i.supplier_id
LEFT JOIN ingredients g ON g.id = l.ingredient_id AND l.match_status = 'confirmed'
CROSS JOIN LATERAL (
    SELECT CASE WHEN i.prices_include_vat
                THEN l.line_amount / (1 + COALESCE(l.vat_rate, 0) / 100)
                ELSE l.line_amount END AS net_amount
) n
WHERE i.status = 'approved' AND i.issue_date IS NOT NULL
"""


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("happened_on", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('price_jump', 'cheaper_supplier', 'over_target')", name=op.f("ck_alerts_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_alerts_dedupe_key")),
    )
    op.create_table(
        "recipes",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("menu_price_gross", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("vat_rate", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("target_food_cost_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column("yield_quantity", sa.Numeric(precision=18, scale=4), server_default=sa.text("1"), nullable=False),
        sa.Column("yield_unit", sa.String(length=10), server_default="piece", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "kind <> 'dish' OR (menu_price_gross IS NOT NULL AND vat_rate IS NOT NULL)",
            name=op.f("ck_recipes_dish_has_price"),
        ),
        sa.CheckConstraint("kind IN ('dish', 'preparation')", name=op.f("ck_recipes_kind")),
        sa.CheckConstraint("yield_unit IN ('g', 'ml', 'piece')", name=op.f("ck_recipes_yield_unit")),
        sa.CheckConstraint(
            "target_food_cost_pct IS NULL OR (target_food_cost_pct > 0 AND target_food_cost_pct < 100)",
            name=op.f("ck_recipes_target_range"),
        ),
        sa.CheckConstraint("yield_quantity > 0", name=op.f("ck_recipes_yield_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipes")),
        sa.UniqueConstraint("key", name=op.f("uq_recipes_key")),
    )
    op.create_table(
        "recipe_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("recipe_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ingredient_id", sa.BigInteger(), nullable=True),
        sa.Column("sub_recipe_id", sa.BigInteger(), nullable=True),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit", sa.String(length=10), nullable=False),
        sa.Column("waste_pct", sa.Numeric(precision=5, scale=2), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("unit IN ('g', 'kg', 'ml', 'cl', 'l', 'piece')", name=op.f("ck_recipe_items_unit")),
        sa.CheckConstraint(
            "(ingredient_id IS NULL) <> (sub_recipe_id IS NULL)", name=op.f("ck_recipe_items_one_source")
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_recipe_items_quantity_positive")),
        sa.CheckConstraint(
            "sub_recipe_id IS NULL OR sub_recipe_id <> recipe_id", name=op.f("ck_recipe_items_not_itself")
        ),
        sa.CheckConstraint("waste_pct >= 0 AND waste_pct < 100", name=op.f("ck_recipe_items_waste_range")),
        sa.ForeignKeyConstraint(
            ["ingredient_id"],
            ["ingredients.id"],
            name=op.f("fk_recipe_items_ingredient_id_ingredients"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["recipe_id"], ["recipes.id"], name=op.f("fk_recipe_items_recipe_id_recipes"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sub_recipe_id"], ["recipes.id"], name=op.f("fk_recipe_items_sub_recipe_id_recipes"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipe_items")),
        sa.UniqueConstraint("recipe_id", "position", name=op.f("uq_recipe_items_recipe_id_position")),
    )

    op.execute(PURCHASE_VIEW)


def downgrade() -> None:
    op.execute("DROP VIEW v_purchase_lines")
    op.drop_table("recipe_items")
    op.drop_table("recipes")
    op.drop_table("alerts")
