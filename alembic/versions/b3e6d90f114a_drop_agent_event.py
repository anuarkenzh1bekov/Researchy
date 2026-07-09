"""drop agent_event — the Redis Stream is the event log now

The Pub/Sub + DB-mirror design dual-wrote every progress event; with Redis
Streams the stream itself is both the replay log and the live feed, so the
table has no readers left.

Revision ID: b3e6d90f114a
Revises: a1f4c8d27e55
Create Date: 2026-07-09 13:00:00.000000

"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = 'b3e6d90f114a'
down_revision = 'a1f4c8d27e55'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(op.f('ix_agent_event_task_id'), table_name='agent_event')
    op.drop_index(op.f('ix_agent_event_created_at'), table_name='agent_event')
    op.drop_table('agent_event')


def downgrade() -> None:
    # schema only — the dropped rows are gone (they were a mirror of ephemeral
    # progress events; nothing reconstructs them).
    op.create_table(
        'agent_event',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('task_id', sa.Uuid(), nullable=False),
        sa.Column('agent_name', sa.String(), nullable=False),
        sa.Column('event_type', sa.String(), nullable=False),
        sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_agent_event_created_at'), 'agent_event', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_event_task_id'), 'agent_event', ['task_id'], unique=False)
