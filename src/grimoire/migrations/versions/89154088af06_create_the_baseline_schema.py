"""create the baseline schema

Revision ID: 89154088af06
Revises:
Create Date: 2026-11-06 21:00:00.000000

The schema as created by ``SQLModel.metadata.create_all`` before the revision
history existed. Databases that predate Alembic are stamped with this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "89154088af06"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "action_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("action_slug", sa.String(), nullable=False),
        sa.Column("action_name", sa.String(), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_action_run_action_slug"), "action_run", ["action_slug"], unique=False)
    op.create_table(
        "action_toggle",
        sa.Column("action_slug", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("action_slug"),
    )
    op.create_table(
        "cached_etag",
        sa.Column("endpoint_url", sa.String(), nullable=False),
        sa.Column("etag", sa.String(), nullable=False),
        sa.Column("last_modified", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("endpoint_url"),
    )
    op.create_table(
        "cached_issue",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_comment_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_cached_issue_repo_full_name"), "cached_issue", ["repo_full_name"], unique=False
    )
    op.create_table(
        "cached_pull_request",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("author", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_push_at", sa.DateTime(), nullable=True),
        sa.Column("last_comment_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_cached_pull_request_repo_full_name"),
        "cached_pull_request",
        ["repo_full_name"],
        unique=False,
    )
    op.create_table(
        "cached_repository",
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("default_branch", sa.String(), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("branches_json", sa.String(), nullable=False),
        sa.Column("open_issues", sa.Integer(), nullable=False),
        sa.Column("stale_issues", sa.Integer(), nullable=False),
        sa.Column("open_pull_requests", sa.Integer(), nullable=False),
        sa.Column("stale_pull_requests", sa.Integer(), nullable=False),
        sa.Column("last_commit_at", sa.DateTime(), nullable=True),
        sa.Column("total_branches", sa.Integer(), nullable=False),
        sa.Column("workflow_include_json", sa.String(), nullable=False),
        sa.Column("workflow_exclude_json", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("full_name"),
    )
    op.create_table(
        "cached_workflow_status",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("workflow_name", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("run_url", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_cached_workflow_status_repo_full_name"),
        "cached_workflow_status",
        ["repo_full_name"],
        unique=False,
    )
    op.create_table(
        "check_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("check_slug", sa.String(), nullable=False),
        sa.Column("check_name", sa.String(), nullable=False),
        sa.Column("triggered_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_check_run_check_slug"), "check_run", ["check_slug"], unique=False)
    op.create_table(
        "check_toggle",
        sa.Column("check_slug", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("check_slug"),
    )
    op.create_table(
        "action_run_repo",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("output", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["action_run.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_action_run_repo_run_id"), "action_run_repo", ["run_id"], unique=False)
    op.create_table(
        "check_result",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("check_slug", sa.String(), nullable=False),
        sa.Column("check_name", sa.String(), nullable=False),
        sa.Column("repo_full_name", sa.String(), nullable=False),
        sa.Column("branch", sa.String(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("output", sa.String(), nullable=False),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["check_run.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_check_result_check_slug"), "check_result", ["check_slug"], unique=False
    )
    op.create_index(
        op.f("ix_check_result_repo_full_name"), "check_result", ["repo_full_name"], unique=False
    )
    op.create_index(op.f("ix_check_result_run_id"), "check_result", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_check_result_run_id"), table_name="check_result")
    op.drop_index(op.f("ix_check_result_repo_full_name"), table_name="check_result")
    op.drop_index(op.f("ix_check_result_check_slug"), table_name="check_result")
    op.drop_table("check_result")
    op.drop_index(op.f("ix_action_run_repo_run_id"), table_name="action_run_repo")
    op.drop_table("action_run_repo")
    op.drop_table("check_toggle")
    op.drop_index(op.f("ix_check_run_check_slug"), table_name="check_run")
    op.drop_table("check_run")
    op.drop_index(
        op.f("ix_cached_workflow_status_repo_full_name"), table_name="cached_workflow_status"
    )
    op.drop_table("cached_workflow_status")
    op.drop_table("cached_repository")
    op.drop_index(op.f("ix_cached_pull_request_repo_full_name"), table_name="cached_pull_request")
    op.drop_table("cached_pull_request")
    op.drop_index(op.f("ix_cached_issue_repo_full_name"), table_name="cached_issue")
    op.drop_table("cached_issue")
    op.drop_table("cached_etag")
    op.drop_table("action_toggle")
    op.drop_index(op.f("ix_action_run_action_slug"), table_name="action_run")
    op.drop_table("action_run")
