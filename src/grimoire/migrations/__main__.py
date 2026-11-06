"""Autogenerate a new revision: ``python -m grimoire.migrations "message"``.

The models are compared against a scratch database upgraded to the current head, so the
result does not depend on any real ``grimoire.db``.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from alembic import command

from grimoire.database import alembic_config, get_engine, upgrade_database


async def create_revision(message: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        engine = await get_engine(str(Path(tmp) / "autogenerate.db"))
        try:
            await upgrade_database(engine)
            async with engine.connect() as conn:
                await conn.run_sync(
                    lambda sync_conn: command.revision(
                        alembic_config(sync_conn), message=message, autogenerate=True
                    )
                )
        finally:
            await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit('usage: python -m grimoire.migrations "message"')
    asyncio.run(create_revision(sys.argv[1]))
