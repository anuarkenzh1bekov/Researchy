"""drop research_task.embedding — the semantic-recall seam was never built

The column was reserved for embedding-based recall of past research; the
feature stayed on the roadmap and nothing ever wrote or read the column, so
the schema (and the pgvector coupling) shrinks instead. The vector EXTENSION
itself is left in place — dropping extensions a shared database may use is
not a migration's call.

Revision ID: c7a2e51d883b
Revises: b3e6d90f114a
Create Date: 2026-07-09 14:00:00.000000

"""
from __future__ import annotations

import pgvector.sqlalchemy
import sqlalchemy as sa

from alembic import op

revision = 'c7a2e51d883b'
down_revision = 'b3e6d90f114a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('research_task', 'embedding')


def downgrade() -> None:
    op.add_column(
        'research_task',
        sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=True),
    )
