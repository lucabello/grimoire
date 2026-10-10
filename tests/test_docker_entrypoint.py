"""Tests for docker-entrypoint.sh (mise + setup script handling)."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).parent.parent / "docker-entrypoint.sh"

FAKE_MISE = """#!/bin/sh
echo "mise $*" >> "$CALLS_LOG"
case "$1" in
    bin-paths) printf '/tools/a\\n/tools/b\\n' ;;
    install) exit "${FAKE_MISE_INSTALL_RC:-0}" ;;
esac
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    """Environment with a fake ``mise`` on PATH and an isolated config dir."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mise = bin_dir / "mise"
    mise.write_text(FAKE_MISE)
    mise.chmod(mise.stat().st_mode | stat.S_IEXEC)

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALLS_LOG": str(tmp_path / "calls.log"),
        "GRIMOIRE_CONFIG": str(config_dir / "config.yaml"),
        "GRIMOIRE_SETUP_SCRIPT": str(tmp_path / "setup.sh"),
    }


async def run_entrypoint(env: dict[str, str], *cmd: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        str(ENTRYPOINT),
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode, stdout.decode()


def calls(env: dict[str, str]) -> list[str]:
    log = Path(env["CALLS_LOG"])
    return log.read_text().splitlines() if log.exists() else []


def mise_toml(env: dict[str, str]) -> Path:
    return Path(env["GRIMOIRE_CONFIG"]).parent / "mise.toml"


async def test_no_mise_toml_skips_mise(env: dict[str, str]) -> None:
    """Without a mise.toml beside the config, mise is never invoked."""
    rc, out = await run_entrypoint(env, "echo", "started")
    assert rc == 0
    assert "started" in out
    assert calls(env) == []


async def test_tools_only_runs_install_without_system_packages(env: dict[str, str]) -> None:
    mise_toml(env).write_text('[tools]\njq = "latest"\n')
    rc, _ = await run_entrypoint(env, "true")
    assert rc == 0
    assert calls(env) == ["mise install", "mise bin-paths"]


async def test_bootstrap_packages_applied_before_install(env: dict[str, str]) -> None:
    mise_toml(env).write_text('[bootstrap.packages]\n"apt:tree" = "latest"\n')
    rc, _ = await run_entrypoint(env, "true")
    assert rc == 0
    assert calls(env)[:2] == [
        "mise bootstrap packages apply --manager apt",
        "mise install",
    ]


async def test_mise_config_points_at_file_next_to_config(env: dict[str, str]) -> None:
    mise_toml(env).write_text("[tools]\n")
    rc, out = await run_entrypoint(env, "sh", "-c", "echo $MISE_GLOBAL_CONFIG_FILE")
    assert rc == 0
    assert str(mise_toml(env)) in out


async def test_tool_paths_are_prepended_to_path(env: dict[str, str]) -> None:
    mise_toml(env).write_text("[tools]\n")
    rc, out = await run_entrypoint(env, "sh", "-c", "echo $PATH")
    assert rc == 0
    assert out.strip().splitlines()[-1].startswith("/tools/a:/tools/b:")


async def test_setup_script_runs_after_mise(env: dict[str, str]) -> None:
    mise_toml(env).write_text("[tools]\n")
    Path(env["GRIMOIRE_SETUP_SCRIPT"]).write_text('echo "setup-ran" >> "$CALLS_LOG"\n')
    rc, _ = await run_entrypoint(env, "true")
    assert rc == 0
    assert calls(env)[-1] == "setup-ran"
    assert calls(env)[0] == "mise install"


async def test_setup_script_runs_without_mise_toml(env: dict[str, str]) -> None:
    """Existing deployments with only data/setup.sh keep working."""
    Path(env["GRIMOIRE_SETUP_SCRIPT"]).write_text('echo "setup-ran" >> "$CALLS_LOG"\n')
    rc, _ = await run_entrypoint(env, "true")
    assert rc == 0
    assert calls(env) == ["setup-ran"]


async def test_failures_are_non_fatal(env: dict[str, str]) -> None:
    """A failing mise install or setup script must not stop the app starting."""
    mise_toml(env).write_text("[tools]\n")
    Path(env["GRIMOIRE_SETUP_SCRIPT"]).write_text("exit 3\n")
    env["FAKE_MISE_INSTALL_RC"] = "1"
    rc, out = await run_entrypoint(env, "echo", "started")
    assert rc == 0
    assert "started" in out
    assert "mise install failed with code 1" in out
    assert "exited with code 3" in out
