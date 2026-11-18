"""Tests for check definition loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from grimoire.checks.loader import load_checks


class TestLoadChecks:
    def test_valid_yaml(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        (checks_dir / "uv-lock-fresh.yaml").write_text(
            "name: UV Lock Fresh\n"
            "description: Ensures uv.lock is fresh\n"
            "targets:\n"
            "  regex: 'acme/.*'\n"
            "script: uv lock --check\n"
            "schedule: '0 */6 * * *'\n"
            "enabled: true\n"
        )

        checks = load_checks(tmp_path)
        assert len(checks) == 1
        c = checks[0]
        assert c.name == "UV Lock Fresh"
        assert c.slug == "uv-lock-fresh"
        assert c.description == "Ensures uv.lock is fresh"
        assert c.targets.regex == "acme/.*"
        assert c.script == "uv lock --check"
        assert c.schedule == "0 */6 * * *"
        assert c.enabled is True

    def test_spaces_parsed_and_default_to_empty(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        base = "description: d\ntargets:\n  regex: '.*'\nscript: 'true'\n"
        (checks_dir / "scoped.yaml").write_text(f"name: Scoped\nspaces: [a, b]\n{base}")
        (checks_dir / "global.yaml").write_text(f"name: Global\n{base}")

        by_slug = {c.slug: c for c in load_checks(tmp_path)}
        assert by_slug["scoped"].spaces == ["a", "b"]
        assert by_slug["global"].spaces == []

    def test_slug_derived_from_filename(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        (checks_dir / "my-cool-check.yaml").write_text(
            "name: My Cool Check\n"
            "description: Does stuff\n"
            "targets:\n"
            "  list:\n"
            "    - acme/alpha\n"
            "script: echo ok\n"
        )

        checks = load_checks(tmp_path)
        assert checks[0].slug == "my-cool-check"

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        (checks_dir / "bad.yaml").write_text(
            "name: Bad Check\n# missing description, targets, script\n"
        )

        with pytest.raises(Exception):
            load_checks(tmp_path)

    def test_empty_directory(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        assert load_checks(tmp_path) == []

    def test_missing_directory(self, tmp_path: Path) -> None:
        assert load_checks(tmp_path) == []

    def test_optional_schedule_defaults_none(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        (checks_dir / "simple.yaml").write_text(
            "name: Simple\n"
            "description: A simple check\n"
            "targets:\n"
            "  list:\n"
            "    - acme/repo\n"
            "script: echo 1\n"
        )
        checks = load_checks(tmp_path)
        assert checks[0].schedule is None
        assert checks[0].enabled is True

    def test_multiple_checks_sorted(self, tmp_path: Path) -> None:
        checks_dir = tmp_path / "checks"
        checks_dir.mkdir()
        for name in ("beta-check.yaml", "alpha-check.yaml"):
            (checks_dir / name).write_text(
                f"name: {name}\ndescription: test\ntargets:\n  list:\n    - a/b\nscript: echo ok\n"
            )
        checks = load_checks(tmp_path)
        assert [c.slug for c in checks] == ["alpha-check", "beta-check"]


class TestBundledCheckDefinitions:
    """Validate the YAML check definitions shipped in data/checks/."""

    @staticmethod
    def _data_dir() -> Path:
        return Path(__file__).resolve().parents[2] / "data"

    def test_watchdog_loads(self) -> None:
        checks = load_checks(self._data_dir())
        watchdog = next((c for c in checks if c.slug == "watchdog"), None)
        assert watchdog is not None
        assert watchdog.name == "Watchdog"
        assert watchdog.targets.regex == ".*"
        assert watchdog.schedule == "0 */8 * * *"
        assert watchdog.enabled is True

    def test_charmcraft_fetch_lib_loads(self) -> None:
        checks = load_checks(self._data_dir())
        charm = next((c for c in checks if c.slug == "charmcraft-fetch-lib"), None)
        assert charm is not None
        assert charm.name == "Charm Libraries up-to-date"
        assert charm.targets.regex == "-operator$"
        assert "charmcraft fetch-lib" in charm.script
        assert charm.schedule == "0 */8 * * *"
        assert charm.enabled is True
