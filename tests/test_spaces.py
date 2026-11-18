"""Tests for grimoire.spaces."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from grimoire.actions.loader import ActionDefinition
from grimoire.checks.loader import CheckDefinition
from grimoire.config import SpaceConfig, StaticRepoSource
from grimoire.models import RepoMembership, TrackedRepository
from grimoire.spaces import (
    ALL_SPACES,
    UNASSIGNED,
    SpaceSelection,
    list_spaces,
    parse_space_ids,
    set_spaces_config,
    validate_definition_spaces,
)
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


# ---------------------------------------------------------------------------
# SpaceSelection, parse_space_ids, list_spaces
# ---------------------------------------------------------------------------


def _repo(name: str, *memberships: RepoMembership) -> TrackedRepository:
    return TrackedRepository(full_name=name, memberships=list(memberships))


# `shared` is top-level and in both spaces with different workflow filters; `solo` is only
# in space a; `loose` is only top-level (unassigned).
SHARED = _repo(
    "o/shared",
    RepoMembership(),
    RepoMembership(space="a", workflow_include=["CI"]),
    RepoMembership(space="b", workflow_include=["Release"]),
)
SOLO = _repo("o/solo", RepoMembership(space="a"))
LOOSE = _repo("o/loose", RepoMembership())


class TestSpaceSelectionIncludes:
    def test_all_includes_everything(self) -> None:
        assert all(ALL_SPACES.includes(r) for r in (SHARED, SOLO, LOOSE))

    def test_one_space(self) -> None:
        sel = SpaceSelection(frozenset({"b"}))
        assert [r.full_name for r in (SHARED, SOLO, LOOSE) if sel.includes(r)] == ["o/shared"]

    def test_several_spaces_union(self) -> None:
        sel = SpaceSelection(frozenset({"a", "b"}))
        assert [r.full_name for r in (SHARED, SOLO, LOOSE) if sel.includes(r)] == [
            "o/shared",
            "o/solo",
        ]

    def test_unassigned_only_matches_repos_without_a_space(self) -> None:
        sel = SpaceSelection(frozenset({UNASSIGNED}))
        assert [r.full_name for r in (SHARED, SOLO, LOOSE) if sel.includes(r)] == ["o/loose"]

    def test_unassigned_together_with_a_space(self) -> None:
        sel = SpaceSelection(frozenset({UNASSIGNED, "b"}))
        assert [r.full_name for r in (SHARED, SOLO, LOOSE) if sel.includes(r)] == [
            "o/shared",
            "o/loose",
        ]


class TestWorkflowVisible:
    def test_all_is_the_union_of_every_membership(self) -> None:
        assert ALL_SPACES.workflow_visible(SHARED, "Lint")  # the top-level membership allows all
        assert ALL_SPACES.workflow_visible(SHARED, "CI")

    def test_single_space_uses_only_that_spaces_filter(self) -> None:
        only_a = SpaceSelection(frozenset({"a"}))
        assert only_a.workflow_visible(SHARED, "CI")
        assert not only_a.workflow_visible(SHARED, "Release")
        assert not only_a.workflow_visible(SHARED, "Lint")

    def test_several_spaces_union_of_their_filters(self) -> None:
        both = SpaceSelection(frozenset({"a", "b"}))
        assert both.workflow_visible(SHARED, "CI")
        assert both.workflow_visible(SHARED, "Release")
        assert not both.workflow_visible(SHARED, "Lint")

    def test_unassigned_uses_the_top_level_membership_of_an_unassigned_repo(self) -> None:
        sel = SpaceSelection(frozenset({UNASSIGNED}))
        assert sel.workflow_visible(LOOSE, "Anything")

    def test_unassigned_does_not_expose_the_top_level_filter_of_a_repo_in_a_space(self) -> None:
        sel = SpaceSelection(frozenset({UNASSIGNED}))
        assert not sel.workflow_visible(SHARED, "Lint")

    def test_repo_without_memberships_shows_everything(self) -> None:
        sel = SpaceSelection(frozenset({"a"}))
        assert sel.workflow_visible(TrackedRepository(full_name="o/new"), "CI")


class TestParseSpaceIds:
    KNOWN = {"a", "b", UNASSIGNED}

    @pytest.mark.parametrize("raw", [None, "", ",", "nope", "nope,other"])
    def test_nothing_usable_means_all(self, raw: str | None) -> None:
        assert parse_space_ids(raw, self.KNOWN) is None

    def test_unknown_ids_are_dropped_silently(self) -> None:
        assert parse_space_ids("a,nope", self.KNOWN) == frozenset({"a"})

    def test_whitespace_is_trimmed(self) -> None:
        assert parse_space_ids(" a , b ", self.KNOWN | {"c"}) == frozenset({"a", "b"})

    def test_every_known_id_normalises_to_all(self) -> None:
        assert parse_space_ids("a,b,_unassigned", self.KNOWN) is None

    def test_every_space_but_not_unassigned_is_a_real_selection(self) -> None:
        assert parse_space_ids("a,b", self.KNOWN) == frozenset({"a", "b"})

    def test_every_space_is_all_when_unassigned_is_not_offered(self) -> None:
        assert parse_space_ids("a,b", {"a", "b"}) is None

    def test_no_known_ids_means_all(self) -> None:
        assert parse_space_ids("a", set()) is None


class TestListSpaces:
    @pytest.fixture(autouse=True)
    def _reset(self) -> Iterator[None]:
        yield
        set_spaces_config([])

    @staticmethod
    def _spaces() -> list[SpaceConfig]:
        source = [StaticRepoSource(repo="o/x")]
        return [
            SpaceConfig(
                id="a", name="Alpha", color="#112233", description="d", repositories=source
            ),
            SpaceConfig(id="b", name="Beta", repositories=source),
        ]

    def test_empty_without_configured_spaces(self) -> None:
        set_spaces_config([], lambda: [LOOSE])
        assert list_spaces() == []

    def test_counts_and_unassigned_entry(self) -> None:
        set_spaces_config(self._spaces(), lambda: [SHARED, SOLO, LOOSE])
        infos = list_spaces()
        assert [(i.id, i.name, i.color, i.repo_count) for i in infos] == [
            ("a", "Alpha", "#112233", 2),
            ("b", "Beta", None, 1),
            (UNASSIGNED, "Unassigned", None, 1),
        ]
        assert infos[0].description == "d"

    def test_no_unassigned_entry_without_unassigned_repos(self) -> None:
        set_spaces_config(self._spaces(), lambda: [SOLO])
        assert [i.id for i in list_spaces()] == ["a", "b"]
