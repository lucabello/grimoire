"""Load check definitions from YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from grimoire.targeting import TargetSpec


class CheckDefinition(BaseModel):
    """A single check definition loaded from YAML."""

    name: str
    slug: str = ""  # auto-derived from filename
    description: str
    targets: TargetSpec
    script: str
    schedule: str | None = None
    enabled: bool = True
    severity: Literal["warning", "error"] = "error"
    spaces: list[str] = []  # listing only (never targeting); empty = listed under every space


def load_checks(data_dir: Path) -> list[CheckDefinition]:
    """Load all YAML check definitions from ``{data_dir}/checks/``.

    Derives slug from the filename (e.g. ``uv-lock-fresh.yaml`` → ``uv-lock-fresh``).
    Validates slug uniqueness and raises on any validation error.
    Returns an empty list if the directory doesn't exist or is empty.
    """
    checks_dir = data_dir / "checks"
    if not checks_dir.is_dir():
        return []

    seen_slugs: dict[str, Path] = {}
    checks: list[CheckDefinition] = []

    for yaml_file in sorted(checks_dir.glob("*.yaml")):
        slug = yaml_file.stem
        if slug in seen_slugs:
            raise ValueError(f"Duplicate check slug '{slug}': {seen_slugs[slug]} and {yaml_file}")
        seen_slugs[slug] = yaml_file

        with open(yaml_file) as f:
            raw = yaml.safe_load(f)

        if not isinstance(raw, dict):
            raise ValueError(f"Expected a YAML mapping in {yaml_file}, got {type(raw).__name__}")

        raw["slug"] = slug
        checks.append(CheckDefinition.model_validate(raw))

    return checks
