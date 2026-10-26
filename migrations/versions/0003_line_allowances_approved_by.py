"""Line allowances and charges, and who approved an invoice (step 3, validation).

- invoice_lines.allowance_amount / charge_amount: discounts and surcharges on a line (EN 16931
  BG-27 / BG-28). Official samples have them, and the line check (quantity x price + charges -
  allowances = line amount) needs them. They also matter for the real price of an ingredient.
- invoices.approved_by: 'auto' when the validation step approved the invoice, otherwise the
  reviewer. An approved invoice must now say who approved it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26 18:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_NAME = "ck_invoices_approved_has_date"


def upgrade() -> None:
    op.add_column(
        "invoice_lines",
        sa.Column("allowance_amount", sa.Numeric(precision=14, scale=2), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "invoice_lines",
        sa.Column("charge_amount", sa.Numeric(precision=14, scale=2), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("invoices", sa.Column("approved_by", sa.Text(), nullable=True))
    # Invoices approved before this migration (none in practice) count as approved automatically.
    op.execute("UPDATE invoices SET approved_by = 'auto' WHERE status = 'approved'")
    # op.f(): the name is final, the naming convention must not add its prefix again
    op.drop_constraint(op.f(CHECK_NAME), "invoices", type_="check")
    op.create_check_constraint(
        op.f(CHECK_NAME),
        "invoices",
        "status <> 'approved' OR (issue_date IS NOT NULL AND approved_at IS NOT NULL AND approved_by IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK_NAME), "invoices", type_="check")
    op.create_check_constraint(
        op.f(CHECK_NAME), "invoices", "status <> 'approved' OR (issue_date IS NOT NULL AND approved_at IS NOT NULL)"
    )
    op.drop_column("invoices", "approved_by")
    op.drop_column("invoice_lines", "charge_amount")
    op.drop_column("invoice_lines", "allowance_amount")
