"""Shared test fixtures for Grimoire."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from grimoire.app import create_app


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Write a minimal valid config.yaml to a temp directory and return its path."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        textwrap.dedent("""\
        github:
          token: "ghp_test_token_123"

        repositories:
          - repo: "owner/repo1"
            branches: ["main", "develop"]
          - repo: "owner/repo2"

        staleness:
          pull_requests_days: 14
          issues_days: 180

        refresh_schedule: "*/10 * * * *"
        data_dir: "{data_dir}"
        workspace_dir: "{workspace_dir}"
        database_path: "{db_path}"
        log_file: "{log_path}"
        """).format(
            data_dir=tmp_path / "data",
            workspace_dir=tmp_path / "workspace",
            db_path=tmp_path / "grimoire.db",
            log_path=tmp_path / "grimoire.log",
        )
    )
    (tmp_path / "data" / "checks").mkdir(parents=True)
    (tmp_path / "data" / "actions").mkdir(parents=True)
    return config_file


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Provide an async HTTP client wired to the FastAPI test app."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
