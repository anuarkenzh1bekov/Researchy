"""source docs

Revision ID: 83671393f124
Revises: b9760c7c55e4
Create Date: 2026-07-06 02:13:58.740950

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = '83671393f124'
down_revision = 'b9760c7c55e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_task",
        sa.Column("source_docs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_task", "source_docs")
