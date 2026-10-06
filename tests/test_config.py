"""Tests for configuration loading and validation."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from grimoire.config import (
    GrimoireConfig,
    StaticRepoSource,
    TeamRepoSource,
    _get_xdg_config_path,
    _get_xdg_data_dir,
    load_config,
    resolve_env_vars,
)


class TestResolveEnvVars:
    """Tests for the env-var resolution utility."""

    def test_resolves_string_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert resolve_env_vars("${MY_TOKEN}") == "secret123"

    def test_ignores_plain_strings(self) -> None:
        assert resolve_env_vars("just a string") == "just a string"

    def test_ignores_partial_references(self) -> None:
        assert resolve_env_vars("prefix_${PARTIAL}") == "prefix_${PARTIAL}"

    def test_raises_on_unset_env_var(self) -> None:
        with pytest.raises(ValueError, match="not set"):
            resolve_env_vars("${DEFINITELY_NOT_SET_12345}")

    def test_resolves_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DB_HOST", "localhost")
        data = {"outer": {"inner": "${DB_HOST}"}}
        assert resolve_env_vars(data) == {"outer": {"inner": "localhost"}}

    def test_resolves_list_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ITEM", "resolved")
        assert resolve_env_vars(["${ITEM}", "plain"]) == ["resolved", "plain"]

    def test_passes_through_non_strings(self) -> None:
        assert resolve_env_vars(42) == 42
        assert resolve_env_vars(None) is None
        assert resolve_env_vars(True) is True


class TestLoadConfig:
    """Tests for loading and validating config.yaml."""

    def test_loads_valid_config(self, tmp_config: Path) -> None:
        config = load_config(tmp_config)
        assert isinstance(config, GrimoireConfig)
        assert config.github.token == "ghp_test_token_123"
        assert len(config.repositories) == 2
        assert config.staleness.pull_requests_days == 14
        assert config.staleness.issues_days == 180
        assert config.refresh_schedule == "*/10 * * * *"

    def test_static_repo_with_branches(self, tmp_config: Path) -> None:
        config = load_config(tmp_config)
        repo = config.repositories[0]
        assert isinstance(repo, StaticRepoSource)
        assert repo.repo == "owner/repo1"
        assert repo.branches == ["main", "develop"]

    def test_static_repo_default_branches(self, tmp_config: Path) -> None:
        config = load_config(tmp_config)
        repo = config.repositories[1]
        assert isinstance(repo, StaticRepoSource)
        assert repo.repo == "owner/repo2"
        assert repo.branches == []

    def test_team_repo_source(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "ghp_test"
            repositories:
              - team: "org/my-team"
                exclude:
                  - "org/excluded"
            """)
        )
        config = load_config(config_file)
        repo_source = config.repositories[0]
        assert isinstance(repo_source, TeamRepoSource)
        assert repo_source.team == "org/my-team"
        assert repo_source.exclude == ["org/excluded"]

    def test_env_var_in_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_GH_TOKEN", "ghp_from_env")
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "${TEST_GH_TOKEN}"
            repositories:
              - repo: "owner/repo"
            """)
        )
        config = load_config(config_file)
        assert config.github.token == "ghp_from_env"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_empty_repositories_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories: []
            """)
        )
        with pytest.raises(Exception, match="[Aa]t least one"):
            load_config(config_file)

    def test_missing_token_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github: {}
            repositories:
              - repo: "owner/repo"
            """)
        )
        with pytest.raises(Exception):
            load_config(config_file)

    def test_invalid_repo_source_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - invalid_key: "something"
            """)
        )
        with pytest.raises(ValueError, match="repo.*team"):
            load_config(config_file)

    def test_git_config_optional(self, tmp_config: Path) -> None:
        config = load_config(tmp_config)
        assert config.git is None

    def test_git_config_parsed(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            git:
              user:
                name: "Test User"
                email: "test@example.com"
              signing:
                key_path: "/keys/id_ed25519"
                format: "ssh"
              ssh_known_hosts: "/keys/known_hosts"
            repositories:
              - repo: "owner/repo"
            """)
        )
        config = load_config(config_file)
        assert config.git is not None
        assert config.git.user.name == "Test User"
        assert config.git.signing is not None
        assert config.git.signing.format == "ssh"

    def test_defaults_applied(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - repo: "owner/repo"
            """)
        )
        # Set HOME to tmp_path for predictable XDG paths
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)

        config = load_config(config_file)
        assert config.staleness.pull_requests_days == 30
        assert config.staleness.issues_days == 365
        assert config.staleness.problematic_stale_issues_pct == 20
        assert config.staleness.problematic_stale_prs_pct == 20
        assert config.refresh_schedule == "*/5 * * * *"
        # XDG data dir defaults
        expected_data_dir = tmp_path / ".local" / "share" / "grimoire" / "data"
        assert config.data_dir == expected_data_dir

    def test_staleness_thresholds_parsed(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - repo: "owner/repo"
            staleness:
              pull_requests_days: 14
              issues_days: 180
              problematic_stale_issues_pct: 30
              problematic_stale_prs_pct: 10
            """)
        )
        config = load_config(config_file)
        assert config.staleness.problematic_stale_issues_pct == 30
        assert config.staleness.problematic_stale_prs_pct == 10

    def test_config_from_env_var(self, tmp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(tmp_config))
        config = load_config()
        assert config.github.token == "ghp_test_token_123"

    def test_non_yaml_file_raises(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("just a plain string")
        with pytest.raises(ValueError, match="mapping"):
            load_config(config_file)

    def test_static_repo_with_workflow_filter(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - repo: "owner/repo"
                workflows:
                  include: ["CI", "Tests *"]
                  exclude: ["Publish *"]
            """)
        )
        config = load_config(config_file)
        repo = config.repositories[0]
        assert isinstance(repo, StaticRepoSource)
        assert repo.workflows.include == ["CI", "Tests *"]
        assert repo.workflows.exclude == ["Publish *"]

    def test_team_repo_with_workflow_filter(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - team: "org/my-team"
                workflows:
                  exclude: ["Nightly *"]
            """)
        )
        config = load_config(config_file)
        repo = config.repositories[0]
        assert isinstance(repo, TeamRepoSource)
        assert repo.workflows.include == []
        assert repo.workflows.exclude == ["Nightly *"]

    def test_workflow_filter_defaults_empty(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - repo: "owner/repo"
            """)
        )
        config = load_config(config_file)
        repo = config.repositories[0]
        assert isinstance(repo, StaticRepoSource)
        assert repo.workflows.include == []
        assert repo.workflows.exclude == []


class TestWorkflowMatchesFilter:
    """Tests for the workflow include/exclude filter logic."""

    def test_no_filters_allows_all(self) -> None:
        from grimoire.github.service import _workflow_matches_filter

        assert _workflow_matches_filter("CI", [], []) is True
        assert _workflow_matches_filter("Publish", [], []) is True

    def test_include_only(self) -> None:
        from grimoire.github.service import _workflow_matches_filter

        assert _workflow_matches_filter("CI", ["CI", "Tests *"], []) is True
        assert _workflow_matches_filter("Tests nightly", ["CI", "Tests *"], []) is True
        assert _workflow_matches_filter("Publish", ["CI", "Tests *"], []) is False

    def test_exclude_only(self) -> None:
        from grimoire.github.service import _workflow_matches_filter

        assert _workflow_matches_filter("CI", [], ["Publish *"]) is True
        assert _workflow_matches_filter("Publish release", [], ["Publish *"]) is False

    def test_include_and_exclude(self) -> None:
        from grimoire.github.service import _workflow_matches_filter

        # "Tests CI" matches include, doesn't match exclude → allowed
        assert _workflow_matches_filter("Tests CI", ["Tests *"], ["Tests nightly"]) is True
        # "Tests nightly" matches include AND exclude → excluded
        assert _workflow_matches_filter("Tests nightly", ["Tests *"], ["Tests nightly"]) is False
        # "Publish" doesn't match include → excluded
        assert _workflow_matches_filter("Publish", ["Tests *"], ["Publish"]) is False


class TestConfigPathResolution:
    """Tests for config file path resolution order."""

    def test_xdg_config_path_used_when_no_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """XDG config path is used when ./config.yaml doesn't exist."""
        # Set up XDG config
        xdg_config = tmp_path / "xdg_config"
        grimoire_config = xdg_config / "grimoire" / "config.yaml"
        grimoire_config.parent.mkdir(parents=True)
        grimoire_config.write_text(
            textwrap.dedent("""\
            github:
              token: "xdg_token"
            repositories:
              - repo: "owner/repo"
            """)
        )

        # Change to a directory without config.yaml
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        monkeypatch.delenv("GRIMOIRE_CONFIG", raising=False)

        config = load_config()
        assert config.github.token == "xdg_token"

    def test_local_config_takes_precedence_over_xdg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """./config.yaml takes precedence over XDG config path."""
        # Set up XDG config
        xdg_config = tmp_path / "xdg_config"
        xdg_grimoire = xdg_config / "grimoire" / "config.yaml"
        xdg_grimoire.parent.mkdir(parents=True)
        xdg_grimoire.write_text(
            textwrap.dedent("""\
            github:
              token: "xdg_token"
            repositories:
              - repo: "owner/xdg-repo"
            """)
        )

        # Set up local config
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        local_config = local_dir / "config.yaml"
        local_config.write_text(
            textwrap.dedent("""\
            github:
              token: "local_token"
            repositories:
              - repo: "owner/local-repo"
            """)
        )

        monkeypatch.chdir(local_dir)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
        monkeypatch.delenv("GRIMOIRE_CONFIG", raising=False)

        config = load_config()
        assert config.github.token == "local_token"

    def test_env_var_takes_precedence_over_local(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GRIMOIRE_CONFIG env var takes precedence over ./config.yaml."""
        # Set up env var config
        env_config = tmp_path / "env_config.yaml"
        env_config.write_text(
            textwrap.dedent("""\
            github:
              token: "env_token"
            repositories:
              - repo: "owner/env-repo"
            """)
        )

        # Set up local config
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        local_config = local_dir / "config.yaml"
        local_config.write_text(
            textwrap.dedent("""\
            github:
              token: "local_token"
            repositories:
              - repo: "owner/local-repo"
            """)
        )

        monkeypatch.chdir(local_dir)
        monkeypatch.setenv("GRIMOIRE_CONFIG", str(env_config))

        config = load_config()
        assert config.github.token == "env_token"

    def test_xdg_default_path_without_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to ~/.config/grimoire/config.yaml when XDG_CONFIG_HOME not set."""

        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        expected = tmp_path / ".config" / "grimoire" / "config.yaml"
        assert _get_xdg_config_path() == expected

    def test_xdg_data_dir_default_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Falls back to ~/.local/share/grimoire when XDG_DATA_HOME not set."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        expected = tmp_path / ".local" / "share" / "grimoire"
        assert _get_xdg_data_dir() == expected

    def test_xdg_data_dir_with_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Uses $XDG_DATA_HOME/grimoire when XDG_DATA_HOME is set."""
        xdg_data = tmp_path / "custom_data"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

        expected = xdg_data / "grimoire"
        assert _get_xdg_data_dir() == expected

    def test_path_defaults_use_xdg_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Config path defaults use XDG data directory."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            textwrap.dedent("""\
            github:
              token: "test"
            repositories:
              - repo: "owner/repo"
            """)
        )

        xdg_data = tmp_path / "xdg_data"
        monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

        config = load_config(config_file)
        grimoire_data = xdg_data / "grimoire"
        assert config.data_dir == grimoire_data / "data"
        assert config.workspace_dir == grimoire_data / "workspace"
        assert config.database_path == grimoire_data / "grimoire.db"
        assert config.log_file == grimoire_data / "grimoire.log"


class TestDashboardConfig:
    def test_defaults(self) -> None:
        from grimoire.config import DashboardConfig

        config = DashboardConfig()
        assert config.workflow_groups == []
        assert config.show_other is True

    def test_parses_groups(self) -> None:
        from grimoire.config import DashboardConfig

        config = DashboardConfig.model_validate(
            {"workflow_groups": [{"name": "Release", "match": ["*release*"]}], "show_other": False}
        )
        assert config.workflow_groups[0].name == "Release"
        assert config.show_other is False

    def test_duplicate_names_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from grimoire.config import DashboardConfig

        group = {"name": "A", "match": ["x"]}
        with pytest.raises(ValidationError):
            DashboardConfig.model_validate({"workflow_groups": [group, group]})

    def test_empty_match_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from grimoire.config import DashboardConfig

        with pytest.raises(ValidationError):
            DashboardConfig.model_validate({"workflow_groups": [{"name": "A", "match": []}]})
