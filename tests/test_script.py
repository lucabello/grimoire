"""Tests for the shebang-aware script execution helper."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import psutil
import pytest

from grimoire import targeting
from grimoire.script import create_script_process, kill_process


async def test_no_shebang_uses_shell(tmp_path: Path) -> None:
    """Scripts without a shebang run via /bin/sh (no temp file)."""
    proc, tmp_script = await create_script_process(
        "echo hello",
        cwd=tmp_path,
        env={},
    )
    stdout, _ = await proc.communicate()
    assert tmp_script is None
    assert proc.returncode == 0
    assert b"hello" in stdout


async def test_bash_shebang(tmp_path: Path) -> None:
    """Scripts with a bash shebang are executed via bash."""
    script = '#!/usr/bin/env bash\necho "bash: ${BASH_VERSION}"'
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={},
    )
    stdout, _ = await proc.communicate()
    assert tmp_script is not None
    assert proc.returncode == 0
    assert b"bash:" in stdout
    tmp_script.unlink(missing_ok=True)


async def test_python_shebang(tmp_path: Path) -> None:
    """Scripts with a python3 shebang run via python3."""
    script = '#!/usr/bin/env python3\nimport sys; print(f"py{sys.version_info[0]}")'
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={},
    )
    stdout, _ = await proc.communicate()
    assert tmp_script is not None
    assert proc.returncode == 0
    assert b"py3" in stdout
    tmp_script.unlink(missing_ok=True)


async def test_temp_file_cleaned_up(tmp_path: Path) -> None:
    """Caller can clean up the temp file after process finishes."""
    script = "#!/usr/bin/env bash\necho ok"
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={},
    )
    await proc.communicate()
    assert tmp_script is not None
    assert tmp_script.exists()
    tmp_script.unlink()
    assert not tmp_script.exists()


async def test_shebang_uses_cwd(tmp_path: Path) -> None:
    """The script runs with the given cwd."""
    script = "#!/usr/bin/env bash\npwd"
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={},
    )
    stdout, _ = await proc.communicate()
    assert str(tmp_path) in stdout.decode()
    if tmp_script:
        tmp_script.unlink(missing_ok=True)


async def test_shebang_passes_env(tmp_path: Path) -> None:
    """The script receives the provided environment variables."""
    script = '#!/usr/bin/env bash\necho "val=$MY_VAR"'
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={"MY_VAR": "test123", "PATH": "/usr/bin:/bin"},
    )
    stdout, _ = await proc.communicate()
    assert b"val=test123" in stdout
    if tmp_script:
        tmp_script.unlink(missing_ok=True)


async def test_nonzero_exit_code(tmp_path: Path) -> None:
    """A failing script returns its exit code."""
    script = "#!/usr/bin/env bash\nexit 42"
    proc, tmp_script = await create_script_process(
        script,
        cwd=tmp_path,
        env={},
    )
    await proc.communicate()
    assert proc.returncode == 42
    if tmp_script:
        tmp_script.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Child reaping
# ---------------------------------------------------------------------------

_ENV = {"PATH": "/usr/bin:/bin"}


def _leftover_children() -> list[psutil.Process]:
    """Children of this process that are still alive or defunct."""
    return psutil.Process().children(recursive=True)


def _is_alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def test_repeated_runs_leave_no_children(tmp_path: Path) -> None:
    for _ in range(20):
        proc, tmp_script = await create_script_process("true", cwd=tmp_path, env=_ENV)
        try:
            await proc.communicate()
        finally:
            await kill_process(proc)
    assert _leftover_children() == []


async def test_kill_process_after_timeout_reaps_child_and_group(tmp_path: Path) -> None:
    """A timed-out script, and anything it spawned, is killed and reaped."""
    pidfile = tmp_path / "grandchild.pid"
    script = f"sleep 60 &\necho $! > {pidfile}\nwait"
    for _ in range(5):
        proc, _ = await create_script_process(script, cwd=tmp_path, env=_ENV)
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(proc.communicate(), timeout=0.5)
        finally:
            await kill_process(proc)
        assert proc.returncode is not None
        assert not _is_alive(int(pidfile.read_text()))
    assert _leftover_children() == []


async def test_cancellation_leaves_no_children(tmp_path: Path) -> None:
    """Cancelling a task that runs a script must not strand the child."""

    async def run() -> None:
        proc, _ = await create_script_process("sleep 60", cwd=tmp_path, env=_ENV)
        try:
            await proc.communicate()
        finally:
            await kill_process(proc)

    for _ in range(5):
        task = asyncio.create_task(run())
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _leftover_children() == []


async def test_script_matches_timeout_reaps_child(tmp_path: Path, monkeypatch) -> None:
    """The targeting timeout path kills *and* reaps the script."""
    monkeypatch.setattr(targeting, "_TARGETING_TIMEOUT", 0.3)
    monkeypatch.setattr(targeting, "target_env", lambda *a, **k: _ENV)
    workspace = MagicMock()
    workspace.get_workdir.return_value = tmp_path
    repo = MagicMock(full_name="o/r")
    for _ in range(5):
        assert await targeting._script_matches("sleep 60", workspace, repo, "main") is False
    assert _leftover_children() == []
