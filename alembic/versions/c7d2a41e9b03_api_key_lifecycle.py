"""api key lifecycle: last_used_at + revoked_at

Revision ID: c7d2a41e9b03
Revises: 83671393f124
Create Date: 2026-07-07 12:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = 'c7d2a41e9b03'
down_revision = '83671393f124'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key", sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "api_key", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("api_key", "revoked_at")
    op.drop_column("api_key", "last_used_at")
