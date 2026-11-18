"""Load action definitions from YAML files."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel

from grimoire.targeting import TargetSpec


class ActionDefinition(BaseModel):
    """A single action definition loaded from YAML."""

    name: str
    slug: str = ""  # auto-derived from filename
    description: str
    targets: TargetSpec | None = None  # None = global (run once, no per-repo iteration)
    script: str
    schedule: str | None = None  # cron expression; None = manual-only
    enabled: bool = True
    spaces: list[str] = []  # listing only (never targeting); empty = listed under every space


def load_actions(data_dir: Path) -> list[ActionDefinition]:
    """Load all YAML action definitions from ``{data_dir}/actions/``.

    Derives slug from the filename (e.g. ``update-uv-lock.yaml`` → ``update-uv-lock``).
    Validates slug uniqueness and raises on any validation error.
    Returns an empty list if the directory doesn't exist or is empty.
    """
    actions_dir = data_dir / "actions"
    if not actions_dir.is_dir():
        return []

    seen_slugs: dict[str, Path] = {}
    actions: list[ActionDefinition] = []

    for yaml_file in sorted(actions_dir.glob("*.yaml")):
        slug = yaml_file.stem
        if slug in seen_slugs:
            raise ValueError(f"Duplicate action slug '{slug}': {seen_slugs[slug]} and {yaml_file}")
        seen_slugs[slug] = yaml_file

        with open(yaml_file) as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Expected a YAML mapping in {yaml_file}, got {type(raw).__name__}")

        raw["slug"] = slug
        actions.append(ActionDefinition.model_validate(raw))

    return actions
