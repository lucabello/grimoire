"""Tests for grimoire.spaces."""

from __future__ import annotations

import pytest

from grimoire.actions.loader import ActionDefinition
from grimoire.checks.loader import CheckDefinition
from grimoire.spaces import validate_definition_spaces
from grimoire.targeting import TargetSpec


def _check(slug: str, spaces: list[str]) -> CheckDefinition:
    return CheckDefinition(
        name=slug,
        slug=slug,
        description="d",
        targets=TargetSpec(regex=".*"),
        script="true",
        spaces=spaces,
    )


class TestValidateDefinitionSpaces:
    def test_known_and_global_definitions_pass(self) -> None:
        validate_definition_spaces(
            "check", [_check("a", ["x", "y"]), _check("b", [])], {"x", "y", "z"}
        )

    def test_unknown_id_names_the_definition_and_the_id(self) -> None:
        with pytest.raises(ValueError, match=r"Unknown space 'nope' in check 'lint'.*x"):
            validate_definition_spaces("check", [_check("lint", ["x", "nope"])], {"x"})

    def test_works_for_actions(self) -> None:
        action = ActionDefinition(name="a", slug="bump", description="d", script="true")
        action.spaces = ["gone"]
        with pytest.raises(ValueError, match=r"Unknown space 'gone' in action 'bump'"):
            validate_definition_spaces("action", [action], {"x"})

    def test_unassigned_pseudo_space_is_not_valid(self) -> None:
        with pytest.raises(ValueError, match="_unassigned"):
            validate_definition_spaces("check", [_check("lint", ["_unassigned"])], {"x"})

    def test_no_spaces_configured_says_so(self) -> None:
        with pytest.raises(ValueError, match="none configured"):
            validate_definition_spaces("check", [_check("lint", ["x"])], set())
