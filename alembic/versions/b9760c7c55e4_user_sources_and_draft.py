"""user sources and draft

Revision ID: b9760c7c55e4
Revises: f9d931baade6
Create Date: 2026-07-05 20:05:15.658658

"""
from __future__ import annotations

import sqlalchemy as sa
import sqlmodel  # noqa: F401 — SQLModel str columns render as sqlmodel.sql.sqltypes.AutoString
from sqlalchemy.dialects import postgresql

from alembic import op

revision = 'b9760c7c55e4'
down_revision = 'f9d931baade6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_task",
        sa.Column("source_urls", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "research_task",
        sa.Column("scrape_report", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "research_task",
        sa.Column("draft_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("research_task", "draft_text")
    op.drop_column("research_task", "scrape_report")
    op.drop_column("research_task", "source_urls")
