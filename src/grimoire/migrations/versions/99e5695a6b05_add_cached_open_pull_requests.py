"""add cached open pull requests

Revision ID: 99e5695a6b05
Revises: ffa5587a73e6
Create Date: 2026-11-06 21:50:53.792075
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "99e5695a6b05"
down_revision: str | None = "ffa5587a73e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cached_open_pull_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("draft", sa.Boolean(), nullable=False),
        sa.Column("labels_json", sa.String(), nullable=False),
        sa.Column("base_ref", sa.String(), nullable=False),
        sa.Column("head_ref", sa.String(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("auto_merge", sa.Boolean(), nullable=False),
        sa.Column("ci_state", sa.String(), nullable=True),
        sa.Column("review_decision", sa.String(), nullable=True),
        sa.Column("merge_state", sa.String(), nullable=True),
        sa.Column("ci_fetched_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("cached_open_pull_request", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_cached_open_pull_request_repo_full_name"),
            ["repo_full_name"],
            unique=False,
        )

    # The pulls lists cached before this revision have no rows in the new table. A stored
    # ETag would make the next fetch answer 304 and keep that empty list, so the PRs page
    # would stay empty until some PR changed. The pattern includes the `&page=N` keys.
    op.execute("DELETE FROM cached_etag WHERE endpoint_url LIKE '%/pulls?%'")


def downgrade() -> None:
    with op.batch_alter_table("cached_open_pull_request", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_cached_open_pull_request_repo_full_name"))

    op.drop_table("cached_open_pull_request")
