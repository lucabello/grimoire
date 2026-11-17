"""Alembic environment.

Migrations always run on a synchronous connection handed over through
``config.attributes["connection"]`` (see ``grimoire.database.alembic_config``), so
they work from inside a running event loop on the application's async engine.
"""

from __future__ import annotations

from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection
from sqlmodel import SQLModel
from sqlmodel.sql.sqltypes import AutoString

import grimoire.database  # noqa: F401  # registers the tables on SQLModel.metadata


def render_item(type_: str, obj: Any, _autogen_context: AutogenContext) -> str | Literal[False]:
    """Render SQLModel's string type as plain SQLAlchemy so revisions stay self-contained."""
    if type_ == "type" and isinstance(obj, AutoString):
        return "sa.String()"
    return False


def run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=SQLModel.metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations(context.config.attributes["connection"])
