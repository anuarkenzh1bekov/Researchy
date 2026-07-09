"""research_task.depth — resolved depth profile persisted by the worker

Revision ID: a1f4c8d27e55
Revises: c7d2a41e9b03
Create Date: 2026-07-09 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = 'a1f4c8d27e55'
down_revision = 'c7d2a41e9b03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable: rows that predate the column (or never got picked up by a
    # worker) simply have no recorded depth.
    op.add_column("research_task", sa.Column("depth", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_task", "depth")
