"""replace workflow filters with repository memberships

Revision ID: 268526b60afc
Revises: 99e5695a6b05
Create Date: 2026-11-18 12:23:50.241297
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "268526b60afc"
down_revision: str | None = "99e5695a6b05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("cached_repository", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("memberships_json", sa.String(), nullable=False, server_default="[]")
        )

    # Every cached repository so far came from top-level sources: one membership without a space
    # carrying the filters it had.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT full_name, workflow_include_json, workflow_exclude_json FROM cached_repository"
        )
    ).fetchall()
    for full_name, include_json, exclude_json in rows:
        membership = {
            "space": None,
            "workflow_include": json.loads(include_json) if include_json else [],
            "workflow_exclude": json.loads(exclude_json) if exclude_json else [],
        }
        connection.execute(
            sa.text("UPDATE cached_repository SET memberships_json = :m WHERE full_name = :n"),
            {"m": json.dumps([membership]), "n": full_name},
        )

    with op.batch_alter_table("cached_repository", schema=None) as batch_op:
        batch_op.alter_column("memberships_json", server_default=None)
        batch_op.drop_column("workflow_include_json")
        batch_op.drop_column("workflow_exclude_json")


def downgrade() -> None:
    with op.batch_alter_table("cached_repository", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("workflow_include_json", sa.VARCHAR(), nullable=False, server_default="[]")
        )
        batch_op.add_column(
            sa.Column("workflow_exclude_json", sa.VARCHAR(), nullable=False, server_default="[]")
        )

    # A single filter pair cannot express several memberships: keep the first one's.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT full_name, memberships_json FROM cached_repository")
    ).fetchall()
    for full_name, memberships_json in rows:
        memberships = json.loads(memberships_json) if memberships_json else []
        first = memberships[0] if memberships else {}
        connection.execute(
            sa.text(
                "UPDATE cached_repository SET workflow_include_json = :i, "
                "workflow_exclude_json = :e WHERE full_name = :n"
            ),
            {
                "i": json.dumps(first.get("workflow_include", [])),
                "e": json.dumps(first.get("workflow_exclude", [])),
                "n": full_name,
            },
        )

    with op.batch_alter_table("cached_repository", schema=None) as batch_op:
        batch_op.alter_column("workflow_include_json", server_default=None)
        batch_op.alter_column("workflow_exclude_json", server_default=None)
        batch_op.drop_column("memberships_json")
