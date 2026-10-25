"""Wider unit prices and signed allowance/charge totals, found with the official sample invoices.

- invoice_lines.unit_price: NUMERIC(18,6) -> NUMERIC(20,10). EN 16931 does not limit the
  decimals of a unit price, and an official XRechnung test case uses 9 (0.061232374).
- invoices: drop the CHECK that allowance_total and charge_total are >= 0. In correction
  invoices every amount is reversed, so these totals can be negative (official ZUGFeRD samples).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25 20:45:09.519062
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "invoice_lines",
        "unit_price",
        existing_type=sa.NUMERIC(precision=18, scale=6),
        type_=sa.Numeric(precision=20, scale=10),
        existing_nullable=False,
    )
    # op.f(): the name is final, the naming convention must not add its prefix again
    op.drop_constraint(op.f("ck_invoices_allowance_charge_non_negative"), "invoices", type_="check")


def downgrade() -> None:
    # Note: going back rounds unit prices to 6 decimals, and fails if negative totals are stored.
    op.create_check_constraint(
        op.f("ck_invoices_allowance_charge_non_negative"), "invoices", "allowance_total >= 0 AND charge_total >= 0"
    )
    op.alter_column(
        "invoice_lines",
        "unit_price",
        existing_type=sa.Numeric(precision=20, scale=10),
        type_=sa.NUMERIC(precision=18, scale=6),
        existing_nullable=False,
    )
