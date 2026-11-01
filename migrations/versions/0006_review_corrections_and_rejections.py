"""Review corrections and rejections (step 8, review screen).

- invoices.corrected_by / corrected_at: a reviewer changed values. The document's raw_extraction
  still holds what was read, so the extraction accuracy on real invoices can be measured later.
- invoices.rejected_reason: why an invoice was rejected (required for status 'rejected').

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-02 01:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invoices", sa.Column("corrected_by", sa.Text(), nullable=True))
    op.add_column("invoices", sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("invoices", sa.Column("rejected_reason", sa.Text(), nullable=True))

    # op.f(): the name is final, the naming convention must not add its prefix again
    op.create_check_constraint(
        op.f("ck_invoices_rejected_has_reason"), "invoices", "status <> 'rejected' OR rejected_reason IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_invoices_rejected_has_reason"), "invoices", type_="check")
    op.drop_column("invoices", "rejected_reason")
    op.drop_column("invoices", "corrected_at")
    op.drop_column("invoices", "corrected_by")
