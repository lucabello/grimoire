"""add last-run details to cached_workflow_status

Revision ID: ffa5587a73e6
Revises: 89154088af06
Create Date: 2026-11-06 21:05:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ffa5587a73e6"
down_revision: str | None = "89154088af06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cached_workflow_status") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("run_attempt", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("event", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("head_sha", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("conclusion", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("run_created_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("run_started_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("run_updated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("cached_workflow_status") as batch_op:
        batch_op.drop_column("run_updated_at")
        batch_op.drop_column("run_started_at")
        batch_op.drop_column("run_created_at")
        batch_op.drop_column("conclusion")
        batch_op.drop_column("head_sha")
        batch_op.drop_column("event")
        batch_op.drop_column("run_attempt")
        batch_op.drop_column("run_id")
