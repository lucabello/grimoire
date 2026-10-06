"""Tests for the `grimoire` CLI entrypoint."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from grimoire.__main__ import main


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv first so monkeypatch restores the original state after main() mutates it.
    monkeypatch.setenv("GRIMOIRE_CONFIG", "")
    monkeypatch.delenv("GRIMOIRE_CONFIG")


def test_defaults() -> None:
    with patch("uvicorn.run") as run:
        main([])
    kwargs = run.call_args.kwargs
    assert kwargs["port"] == 8000
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["factory"] is True
    assert "GRIMOIRE_CONFIG" not in os.environ


def test_config_file_and_port(tmp_path: Path) -> None:
    config = tmp_path / "config.mlops.yaml"
    config.write_text("repositories: []\n")
    with patch("uvicorn.run") as run:
        main(["--config-file", str(config), "--port", "8081"])
    assert run.call_args.kwargs["port"] == 8081
    assert os.environ["GRIMOIRE_CONFIG"] == str(config)


@pytest.mark.parametrize(
    ("given", "expected"),
    [("", ""), ("/grimoire", "/grimoire"), ("grimoire/", "/grimoire"), ("/a/b/", "/a/b")],
)
def test_root_path_is_normalised(given: str, expected: str) -> None:
    with patch("uvicorn.run") as run:
        main(["--root-path", given])
    assert run.call_args.kwargs["root_path"] == expected


def test_missing_config_file_exits(tmp_path: Path) -> None:
    with patch("uvicorn.run") as run, pytest.raises(SystemExit):
        main(["--config-file", str(tmp_path / "nope.yaml")])
    run.assert_not_called()
