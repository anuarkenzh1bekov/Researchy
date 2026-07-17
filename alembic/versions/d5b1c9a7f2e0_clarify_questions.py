"""research_task.clarify_questions — interview intake state for the bot

Revision ID: d5b1c9a7f2e0
Revises: c7a2e51d883b
Create Date: 2026-07-17 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = 'd5b1c9a7f2e0'
down_revision = 'c7a2e51d883b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable JSONB: non-null holds the clarifying questions the bot asked and
    # is awaiting a reply for; cleared to null once answered or skipped. Rows
    # that predate the column simply have none.
    op.add_column(
        "research_task",
        sa.Column("clarify_questions", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_task", "clarify_questions")
