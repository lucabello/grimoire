"""YAML configuration schema and loader for Grimoire."""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from dataconfy import ConfigManager, DataManager
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from typing_extensions import Self

_ENV_VAR_PATTERN = re.compile(r"^\$\{([^}]+)\}$")


def _get_config_manager() -> ConfigManager:
    """Return a ConfigManager for Grimoire (created lazily to respect env vars)."""
    return ConfigManager(app_name="grimoire")


def _get_data_manager() -> DataManager:
    """Return a DataManager for Grimoire (created lazily to respect env vars)."""
    return DataManager(app_name="grimoire")


def resolve_env_vars(raw: object) -> object:
    """Recursively walk a parsed YAML structure and resolve ``${ENV_VAR}`` references.

    Only exact-match string values are resolved (no partial interpolation).
    Raises ``ValueError`` for references to unset environment variables.
    """
    if isinstance(raw, str):
        match = _ENV_VAR_PATTERN.match(raw)
        if match:
            var_name = match.group(1)
            value = os.environ.get(var_name)
            if value is None:
                raise ValueError(
                    f"Environment variable '{var_name}' is referenced in config but not set"
                )
            return value
        return raw
    if isinstance(raw, dict):
        return {k: resolve_env_vars(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return [resolve_env_vars(item) for item in raw]
    return raw


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class GitHubConfig(BaseModel):
    """GitHub API authentication."""

    token: str


class GitUserConfig(BaseModel):
    """Git user identity for commits."""

    name: str
    email: str


class SigningConfig(BaseModel):
    """Commit signing configuration."""

    key_path: Path
    format: Literal["ssh", "gpg"]


class GitConfig(BaseModel):
    """Git identity and signing — optional, only needed for actions that commit/push."""

    user: GitUserConfig
    signing: SigningConfig | None = None
    ssh_known_hosts: Path | None = None


class WorkflowFilter(BaseModel):
    """Include/exclude filter for GitHub Actions workflows (glob patterns on name)."""

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class StaticRepoSource(BaseModel):
    """A single repository specified by full name, with optional branch list."""

    repo: str
    branches: list[str] = Field(default_factory=list)
    workflows: WorkflowFilter = Field(default_factory=WorkflowFilter)


class TeamRepoSource(BaseModel):
    """All repositories belonging to a GitHub team, with optional exclusions."""

    team: str
    exclude: list[str] = Field(default_factory=list)
    workflows: WorkflowFilter = Field(default_factory=WorkflowFilter)


RepoSource = Annotated[Union[StaticRepoSource, TeamRepoSource], Field(discriminator=None)]


class SpaceConfig(BaseModel):
    """A named slice of the tracked repositories that teams can filter the UI by."""

    model_config = ConfigDict(extra="forbid")

    # Ids may not start with "_": that prefix is reserved for pseudo-spaces (_unassigned).
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    description: str = ""
    repositories: list[RepoSource] = Field(min_length=1)

    @field_validator("repositories", mode="before")
    @classmethod
    def parse_repo_sources(cls, value: object) -> object:
        if isinstance(value, list):
            return [_parse_repo_source(v) if isinstance(v, dict) else v for v in value]
        return value


class StalenessConfig(BaseModel):
    """Thresholds for marking issues/PRs as stale."""

    pull_requests_days: int = 30
    issues_days: int = 365

    # Percentage thresholds: highlight stale counts as problematic
    # when stale/open >= this percentage.
    problematic_stale_issues_pct: int = 20
    problematic_stale_prs_pct: int = 20


class BacklogCategoryWeights(BaseModel):
    """Base importance weights for each backlog item category."""

    failing_workflow: float = 100.0
    failing_check_error: float = 80.0
    failing_check_warning: float = 30.0
    stale_pr: float = 50.0
    stale_issue: float = 20.0


class RepositoryWeightRule(BaseModel):
    """A rule mapping repositories to a backlog weight multiplier.

    Exactly one of ``regex`` or ``repos`` must be set.
    - ``regex``: fnmatch glob pattern matched against the full_name.
    - ``repos``: explicit list of full_name strings.
    """

    regex: str | None = None
    repos: list[str] | None = None
    weight: float = 1.0

    @model_validator(mode="after")
    def exactly_one_selector(self) -> Self:
        if (self.regex is None) == (self.repos is None):
            raise ValueError("Exactly one of 'regex' or 'repos' must be set")
        return self


class BacklogConfig(BaseModel):
    """Configuration for the Backlog (prioritised problem list) page."""

    category_weights: BacklogCategoryWeights = Field(default_factory=BacklogCategoryWeights)
    workflow_weights: dict[str, float] = Field(default_factory=dict)
    repository_weights: list[RepositoryWeightRule] = Field(default_factory=list)


class WorkflowGroup(BaseModel):
    """A named dashboard column holding workflows whose name matches any glob pattern."""

    name: str = Field(min_length=1)
    match: list[str] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Workflow group name must not be blank")
        return value


class DashboardConfig(BaseModel):
    """Dashboard presentation options.

    With no ``workflow_groups`` all workflows share a single "Workflows" column.
    Otherwise each group becomes a column (first matching group wins) and
    unmatched workflows go to a trailing "Other" column unless ``show_other`` is false.
    """

    workflow_groups: list[WorkflowGroup] = Field(default_factory=list)
    show_other: bool = True

    @model_validator(mode="after")
    def unique_group_names(self) -> Self:
        names = [group.name for group in self.workflow_groups]
        if len(names) != len(set(names)):
            raise ValueError("Workflow group names must be unique")
        if self.show_other and "Other" in names:
            raise ValueError(
                'Workflow group name "Other" is reserved for the unmatched column; '
                "rename the group or set show_other to false"
            )
        return self


def _clean_value(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"PR group '{field}' values must not be blank")
    return value


class PRGroup(BaseModel):
    """A named set of pull-request matchers selecting open PRs of the tracked repositories.

    Values of one list matcher are alternatives (OR); different matchers combine with AND.
    ``authors`` ignore case, a leading ``app/`` and a trailing ``[bot]`` (``app/renovate``
    matches ``renovate[bot]``). ``title_words`` are case-insensitive substrings. ``repos`` is
    an optional scope: fnmatch patterns on the tracked repository full names (all tracked
    repositories when empty).
    """

    name: str
    authors: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    title_words: list[str] = Field(default_factory=list)
    draft: bool | None = None
    base: str | None = None
    repos: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("PR group name must not be blank")
        return value

    @field_validator("base")
    @classmethod
    def base_valid(cls, value: str | None) -> str | None:
        return None if value is None else _clean_value(value, "base")

    @field_validator("authors", "labels", "title_words", "repos")
    @classmethod
    def entries_valid(cls, values: list[str], info: ValidationInfo) -> list[str]:
        return [_clean_value(value, str(info.field_name)) for value in values]

    @model_validator(mode="after")
    def at_least_one_matcher(self) -> Self:
        if not (
            self.authors
            or self.labels
            or self.title_words
            or self.draft is not None
            or self.base is not None
        ):
            raise ValueError(
                f"PR group '{self.name}' needs at least one matcher: "
                "authors, labels, title_words, draft or base"
            )
        return self


class PRsConfig(BaseModel):
    """Configuration of the PRs page: named groups of open pull requests."""

    groups: list[PRGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_group_names(self) -> Self:
        names = [group.name for group in self.groups]
        if len(names) != len(set(names)):
            raise ValueError("PR group names must be unique")
        return self


def _default_data_dir() -> Path:
    return _get_data_manager().data_dir / "data"


def _default_workspace_dir() -> Path:
    return _get_data_manager().data_dir / "workspace"


def _default_database_path() -> Path:
    return _get_data_manager().data_dir / "grimoire.db"


def _default_log_file() -> Path:
    return _get_data_manager().data_dir / "grimoire.log"


class GrimoireConfig(BaseModel):
    """Top-level application configuration."""

    github: GitHubConfig
    git: GitConfig | None = None

    repositories: list[RepoSource] = Field(default_factory=list)
    spaces: list[SpaceConfig] = Field(default_factory=list)
    # Globs (relative to the main config file's directory) of files holding one space each.
    # load_config expands them and appends the result to ``spaces``.
    space_files: list[str] = Field(default_factory=list)

    staleness: StalenessConfig = Field(default_factory=StalenessConfig)
    backlog: BacklogConfig = Field(default_factory=BacklogConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    prs: PRsConfig = Field(default_factory=PRsConfig)
    refresh_schedule: str = "*/5 * * * *"

    data_dir: Path = Field(default_factory=_default_data_dir)
    workspace_dir: Path = Field(default_factory=_default_workspace_dir)
    database_path: Path = Field(default_factory=_default_database_path)
    log_file: Path = Field(default_factory=_default_log_file)

    @model_validator(mode="after")
    def at_least_one_repo_source(self) -> Self:
        if not self.repositories and not self.spaces:
            raise ValueError("At least one repository source must be configured")
        return self

    @model_validator(mode="after")
    def unique_space_ids(self) -> Self:
        ids = [space.id for space in self.spaces]
        duplicates = sorted({space_id for space_id in ids if ids.count(space_id) > 1})
        if duplicates:
            raise ValueError(f"Duplicate space ids: {', '.join(duplicates)}")
        return self


def _parse_repo_source(raw: dict) -> RepoSource:  # type: ignore[type-arg]
    """Disambiguate a repository source dict into the correct model."""
    if "repo" in raw:
        return StaticRepoSource.model_validate(raw)
    if "team" in raw:
        return TeamRepoSource.model_validate(raw)
    raise ValueError(f"Repository source must have either 'repo' or 'team' key, got: {raw}")


def _get_xdg_config_path() -> Path:
    """Return the XDG config path for Grimoire (via dataconfy)."""
    return _get_config_manager().config_dir / "config.yaml"


def _get_xdg_data_dir() -> Path:
    """Return the XDG data directory for Grimoire (via dataconfy)."""
    return _get_data_manager().data_dir


def load_config(path: Path | None = None) -> GrimoireConfig:
    """Load and validate the Grimoire configuration file.

    Resolution order for the config path:
    1. Explicit ``path`` argument.
    2. ``GRIMOIRE_CONFIG`` environment variable.
    3. ``./config.yaml`` in the current working directory.
    4. ``~/.config/grimoire/config.yaml`` (XDG config path).
    """
    if path is None:
        env_path = os.environ.get("GRIMOIRE_CONFIG")
        if env_path:
            path = Path(env_path)
        elif Path("config.yaml").exists():
            path = Path("config.yaml")
        else:
            path = _get_xdg_config_path()

    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"Expected a YAML mapping at the top level, got {type(raw).__name__}")

    resolved = resolve_env_vars(raw)
    assert isinstance(resolved, dict)

    # Manually parse the heterogeneous repositories list
    raw_repos = resolved.get("repositories", [])
    parsed_repos = [_parse_repo_source(r) for r in raw_repos]
    resolved["repositories"] = parsed_repos

    resolved["spaces"] = _merge_spaces(resolved, base_dir=path.parent)

    return GrimoireConfig.model_validate(resolved)


def _load_space_files(patterns: object, base_dir: Path) -> list[tuple[Path, SpaceConfig]]:
    """Expand ``space_files`` globs and load the single space each matching file holds."""
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise ValueError("space_files must be a list of glob patterns")

    seen: set[Path] = set()
    loaded: list[tuple[Path, SpaceConfig]] = []
    for pattern in patterns:
        for match in sorted(glob.glob(pattern, root_dir=base_dir, recursive=True)):
            file_path = (base_dir / match).resolve()
            if file_path in seen or not file_path.is_file():
                continue
            seen.add(file_path)

            with open(file_path) as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                raise ValueError(
                    f"Space file {file_path} must hold a YAML mapping for one space, "
                    f"got {type(raw).__name__}"
                )
            try:
                space = SpaceConfig.model_validate(resolve_env_vars(raw))
            except ValidationError as exc:
                raise ValueError(f"Invalid space file {file_path}: {exc}") from exc
            loaded.append((file_path, space))
    return loaded


def _merge_spaces(resolved: dict, base_dir: Path) -> list:  # type: ignore[type-arg]
    """Append the spaces from ``space_files`` to the inline ones; ids must be unique."""
    inline = resolved.get("spaces") or []
    if not isinstance(inline, list):
        raise ValueError("spaces must be a list")

    origins: dict[str, str] = {}
    for entry in inline:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            space_id = entry["id"]
            if space_id in origins:
                raise ValueError(f"Duplicate space id '{space_id}': defined twice inline")
            origins[space_id] = "inline"

    spaces: list = list(inline)  # type: ignore[type-arg]
    for file_path, space in _load_space_files(resolved.get("space_files", []), base_dir):
        if space.id in origins:
            raise ValueError(
                f"Duplicate space id '{space.id}': defined in {origins[space.id]} "
                f"and in {file_path}"
            )
        origins[space.id] = str(file_path)
        spaces.append(space)
    return spaces
