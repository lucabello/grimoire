"""Tests for the CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from grimoire.__main__ import _parse_args, main


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("GRIMOIRE_HOST", "GRIMOIRE_PORT", "GRIMOIRE_ROOT_PATH"):
        monkeypatch.delenv(var, raising=False)
    args = _parse_args([])
    assert (args.host, args.port, args.root_path) == ("0.0.0.0", 8000, "")


@pytest.mark.parametrize("raw", ["grimoire", "/grimoire", "/grimoire/", "grimoire/"])
def test_root_path_normalised(raw: str) -> None:
    assert _parse_args(["--root-path", raw]).root_path == "/grimoire"


def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIMOIRE_PORT", "9000")
    monkeypatch.setenv("GRIMOIRE_ROOT_PATH", "/x")
    args = _parse_args([])
    assert (args.port, args.root_path) == (9000, "/x")


def test_missing_config_file_errors(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--config-file", str(tmp_path / "nope.yaml")])


def test_main_passes_options_to_uvicorn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "c.yaml"
    cfg.write_text("")
    monkeypatch.delenv("GRIMOIRE_CONFIG", raising=False)
    with patch("uvicorn.run") as run:
        main(
            [
                "--config-file",
                str(cfg),
                "--port",
                "9001",
                "--host",
                "127.0.0.1",
                "--root-path",
                "/p",
            ]
        )
    kwargs = run.call_args.kwargs
    assert (kwargs["port"], kwargs["host"], kwargs["root_path"]) == (9001, "127.0.0.1", "/p")
    import os

    assert os.environ["GRIMOIRE_CONFIG"] == str(cfg)
    monkeypatch.delenv("GRIMOIRE_CONFIG")
