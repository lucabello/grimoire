"""Tests for the Backlog feature — priority engine and web routes."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from grimoire.config import (
    BacklogCategoryWeights,
    BacklogConfig,
    RepositoryWeightRule,
    StalenessConfig,
)
from grimoire.models import (
    IssueDetail,
    PullRequestDetail,
    RepositoryStats,
    TrackedRepository,
    WorkflowStatus,
)
from grimoire.web.backlog import (
    BacklogCategory,
    BacklogItem,
    _compute_age_factor,
    _days_since,
    _format_age,
    build_backlog_items,
    compute_score,
    export_markdown,
    group_by_repo,
    group_by_type,
    resolve_repo_weight,
)

# ---------------------------------------------------------------------------
# Unit tests for priority scoring
# ---------------------------------------------------------------------------


class TestComputeScore:
    """Tests for the priority score computation."""

    def _default_config(self, **overrides: float) -> BacklogConfig:
        return BacklogConfig(
            category_weights=BacklogCategoryWeights(**overrides),
        )

    def test_default_weights_workflow(self) -> None:
        config = self._default_config()
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=1.0,
            config=config,
        )
        assert score == 100.0  # 100 * 1.0 * 1.0 (age_factor for 0 days)

    def test_repo_priority_multiplier(self) -> None:
        config = self._default_config()
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=3.0,
            age_days=0.0,
            reference_days=1.0,
            config=config,
        )
        assert score == 300.0

    def test_zero_repo_priority_hides_items(self) -> None:
        config = self._default_config()
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=0.0,
            age_days=10.0,
            reference_days=1.0,
            config=config,
        )
        assert score == 0.0

    def test_age_factor_increases_score(self) -> None:
        config = self._default_config()
        score_fresh = compute_score(
            BacklogCategory.STALE_PR,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=30.0,
            config=config,
        )
        score_old = compute_score(
            BacklogCategory.STALE_PR,
            repo_weight=1.0,
            age_days=30.0,
            reference_days=30.0,
            config=config,
        )
        assert score_old > score_fresh
        # At exactly reference_days, age_factor should be 1 + log2(2) = 2.0
        assert score_old == pytest.approx(50.0 * 2.0)

    def test_workflow_weight_override(self) -> None:
        config = BacklogConfig(
            workflow_weights={"Release .*": 2.0},
        )
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=1.0,
            config=config,
            workflow_name="Release Deploy",
        )
        assert score == 200.0  # 100 * 1.0 * 2.0

    def test_workflow_weight_no_match_uses_default(self) -> None:
        config = BacklogConfig(
            workflow_weights={"Release .*": 2.0},
        )
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=1.0,
            config=config,
            workflow_name="CI",
        )
        assert score == 100.0

    def test_workflow_weight_regex_alternation(self) -> None:
        config = BacklogConfig(
            workflow_weights={"^(CI|Build)$": 3.0},
        )
        for name in ("CI", "Build"):
            score = compute_score(
                BacklogCategory.FAILING_WORKFLOW,
                repo_weight=1.0,
                age_days=0.0,
                reference_days=1.0,
                config=config,
                workflow_name=name,
            )
            assert score == 300.0
        # Non-matching
        score = compute_score(
            BacklogCategory.FAILING_WORKFLOW,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=1.0,
            config=config,
            workflow_name="Deploy",
        )
        assert score == 100.0

    def test_custom_category_weight(self) -> None:
        config = self._default_config(stale_issue=5.0)
        score = compute_score(
            BacklogCategory.STALE_ISSUE,
            repo_weight=1.0,
            age_days=0.0,
            reference_days=365.0,
            config=config,
        )
        assert score == 5.0


class TestAgeFactor:
    """Tests for the age factor calculation."""

    def test_zero_age(self) -> None:
        assert _compute_age_factor(0.0, 30.0) == 1.0

    def test_at_reference(self) -> None:
        factor = _compute_age_factor(30.0, 30.0)
        assert factor == pytest.approx(1.0 + math.log2(2.0))

    def test_negative_reference_clamped(self) -> None:
        # Should not crash; treats reference as 1.0
        factor = _compute_age_factor(5.0, -10.0)
        assert factor > 1.0


class TestFormatAge:
    """Tests for the age formatting utility."""

    def test_less_than_day(self) -> None:
        assert _format_age(0.5) == "<1d"

    def test_days(self) -> None:
        assert _format_age(45.0) == "45d"

    def test_years(self) -> None:
        assert _format_age(365.0) == "1y"

    def test_years_and_days(self) -> None:
        assert _format_age(400.0) == "1y 35d"


class TestDaysSince:
    """Tests for the _days_since helper."""

    def test_none_returns_zero(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        assert _days_since(None, now) == 0.0

    def test_naive_datetime_treated_as_utc(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        dt = datetime(2026, 4, 1)  # naive
        result = _days_since(dt, now)
        assert result == 30.0

    def test_future_date_returns_zero(self) -> None:
        now = datetime(2026, 5, 1, tzinfo=timezone.utc)
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert _days_since(dt, now) == 0.0


class TestResolveRepoWeight:
    """Tests for resolve_repo_weight()."""

    def test_no_rules_returns_default(self) -> None:
        config = BacklogConfig()
        assert resolve_repo_weight("org/repo", config) == 1.0

    def test_regex_match(self) -> None:
        config = BacklogConfig(
            repository_weights=[RepositoryWeightRule(regex="org/*", weight=3.0)],
        )
        assert resolve_repo_weight("org/repo", config) == 3.0

    def test_repos_list_match(self) -> None:
        config = BacklogConfig(
            repository_weights=[
                RepositoryWeightRule(repos=["org/important"], weight=5.0),
            ],
        )
        assert resolve_repo_weight("org/important", config) == 5.0
        assert resolve_repo_weight("org/other", config) == 1.0

    def test_last_match_wins(self) -> None:
        config = BacklogConfig(
            repository_weights=[
                RepositoryWeightRule(regex="*", weight=2.0),
                RepositoryWeightRule(repos=["org/special"], weight=10.0),
            ],
        )
        assert resolve_repo_weight("org/special", config) == 10.0
        assert resolve_repo_weight("org/other", config) == 2.0

    def test_no_match_returns_default(self) -> None:
        config = BacklogConfig(
            repository_weights=[
                RepositoryWeightRule(repos=["org/specific"], weight=5.0),
            ],
        )
        assert resolve_repo_weight("other/repo", config) == 1.0


# ---------------------------------------------------------------------------
# Unit tests for item collection
# ---------------------------------------------------------------------------

NOW = datetime(2026, 5, 1, tzinfo=timezone.utc)


def _make_repo(name: str = "acme/api", branches: list[str] | None = None) -> TrackedRepository:
    return TrackedRepository(
        full_name=name,
        default_branch="main",
        branches=branches or ["main"],
        source="static",
    )


def _make_stats(
    name: str = "acme/api",
    workflows: list[WorkflowStatus] | None = None,
    stale_pr_items: list[PullRequestDetail] | None = None,
    stale_issue_items: list[IssueDetail] | None = None,
) -> RepositoryStats:
    return RepositoryStats(
        full_name=name,
        default_branch="main",
        workflows=workflows or [],
        stale_pr_items=stale_pr_items or [],
        stale_issue_items=stale_issue_items or [],
        fetched_at=NOW,
    )


class TestBuildBacklogItems:
    """Tests for the full backlog item builder."""

    def test_empty_cache(self) -> None:
        items = build_backlog_items(
            cache={},
            repos={},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert items == []

    def test_failing_workflow_creates_item(self) -> None:
        wf = WorkflowStatus(
            name="CI",
            branch="main",
            status="failure",
            url="https://github.com/acme/api/actions",
            run_url="https://github.com/acme/api/actions/runs/42",
        )
        repo = _make_repo()
        stats = _make_stats(workflows=[wf])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 1
        assert items[0].category == BacklogCategory.FAILING_WORKFLOW
        assert "CI" in items[0].description
        assert items[0].url == "https://github.com/acme/api/actions/runs/42"
        assert items[0].score == 100.0

    def test_passing_workflow_not_included(self) -> None:
        wf = WorkflowStatus(name="CI", branch="main", status="success", url="")
        repo = _make_repo()
        stats = _make_stats(workflows=[wf])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 0

    def test_stale_pr_creates_item(self) -> None:
        pr = PullRequestDetail(
            number=99,
            title="Fix auth",
            url="https://github.com/acme/api/pull/99",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
            author="alice",
        )
        repo = _make_repo()
        stats = _make_stats(stale_pr_items=[pr])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(pull_requests_days=30),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 1
        assert items[0].category == BacklogCategory.STALE_PR
        assert items[0].number == 99
        # Age is ~61 days, excess over 30d threshold is ~31d
        assert items[0].age_days == pytest.approx(61.0, abs=1.0)

    def test_stale_issue_creates_item(self) -> None:
        issue = IssueDetail(
            number=42,
            title="Old bug",
            url="https://github.com/acme/api/issues/42",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        repo = _make_repo()
        stats = _make_stats(stale_issue_items=[issue])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 1
        assert items[0].category == BacklogCategory.STALE_ISSUE
        assert items[0].number == 42

    def test_pr_with_number_zero_is_excluded(self) -> None:
        """Regression: items with number=0 (invalid) must not appear in backlog."""
        valid_pr = PullRequestDetail(
            number=42,
            title="Valid PR",
            url="https://github.com/acme/api/pull/42",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        invalid_pr = PullRequestDetail(
            number=0,
            title="Ghost PR",
            url="https://github.com/acme/api/pull/0",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        repo = _make_repo()
        stats = _make_stats(stale_pr_items=[valid_pr, invalid_pr])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(pull_requests_days=30),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 1
        assert items[0].number == 42

    def test_issue_with_number_zero_is_excluded(self) -> None:
        """Regression: issues with number=0 (invalid) must not appear in backlog."""
        valid_issue = IssueDetail(
            number=10,
            title="Valid issue",
            url="https://github.com/acme/api/issues/10",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        invalid_issue = IssueDetail(
            number=0,
            title="Ghost issue",
            url="https://github.com/acme/api/issues/0",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        repo = _make_repo()
        stats = _make_stats(stale_issue_items=[valid_issue, invalid_issue])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 1
        assert items[0].number == 10

    def test_items_sorted_by_score_descending(self) -> None:
        """Multiple item types should be sorted highest-score-first."""
        wf = WorkflowStatus(name="CI", branch="main", status="failure", url="", run_url="")
        issue = IssueDetail(
            number=1,
            title="Old",
            url="",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        repo = _make_repo()
        stats = _make_stats(workflows=[wf], stale_issue_items=[issue])
        items = build_backlog_items(
            cache={repo.full_name: stats},
            repos={repo.full_name: repo},
            config=BacklogConfig(),
            staleness=StalenessConfig(),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 2
        assert items[0].score >= items[1].score
        assert items[0].category == BacklogCategory.FAILING_WORKFLOW

    def test_repo_priority_affects_ranking(self) -> None:
        """A low-weight repo's workflow should rank below a high-weight repo's stale PR."""
        wf = WorkflowStatus(name="CI", branch="main", status="failure", url="", run_url="")
        pr = PullRequestDetail(
            number=1,
            title="PR",
            url="",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            last_activity_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        low_repo = _make_repo(name="low/repo")
        high_repo = _make_repo(name="high/repo")
        low_stats = _make_stats(name="low/repo", workflows=[wf])
        high_stats = _make_stats(name="high/repo", stale_pr_items=[pr])

        config = BacklogConfig(
            repository_weights=[
                RepositoryWeightRule(regex="low/*", weight=0.3),
                RepositoryWeightRule(regex="high/*", weight=5.0),
            ],
        )
        items = build_backlog_items(
            cache={"low/repo": low_stats, "high/repo": high_stats},
            repos={"low/repo": low_repo, "high/repo": high_repo},
            config=config,
            staleness=StalenessConfig(pull_requests_days=30),
            check_targets={},
            results_by_key={},
            check_defs=[],
            now=NOW,
        )
        assert len(items) == 2
        assert items[0].repo_full_name == "high/repo"


# ---------------------------------------------------------------------------
# Tests for BacklogItem properties
# ---------------------------------------------------------------------------


class TestBacklogItem:
    """Tests for BacklogItem tier and rendering methods."""

    def test_tier_critical(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.FAILING_WORKFLOW,
            repo_full_name="a/b",
            description="x",
            url="",
            age_days=0,
            score=100,
        )
        assert item.tier == "critical"
        assert item.tier_class == "error"

    def test_tier_high(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.STALE_PR,
            repo_full_name="a/b",
            description="x",
            url="",
            age_days=0,
            score=60,
        )
        assert item.tier == "high"

    def test_tier_medium(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.STALE_ISSUE,
            repo_full_name="a/b",
            description="x",
            url="",
            age_days=0,
            score=25,
        )
        assert item.tier == "medium"

    def test_tier_low(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.STALE_ISSUE,
            repo_full_name="a/b",
            description="x",
            url="",
            age_days=0,
            score=5,
        )
        assert item.tier == "low"

    def test_to_markdown_with_url(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.FAILING_WORKFLOW,
            repo_full_name="acme/api",
            description="CI failing on `main`",
            url="https://github.com/acme/api/runs/1",
            age_days=3.0,
            score=100,
        )
        md = item.to_markdown()
        assert md.startswith("- [ ] **[acme/api]**")
        assert "[View](" in md
        assert "(3d)" in md

    def test_to_markdown_without_url(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.FAILING_CHECK_ERROR,
            repo_full_name="acme/api",
            description="Check 'lint' failing on `main`",
            url="",
            age_days=0.0,
            score=80,
        )
        md = item.to_markdown()
        assert "[View]" not in md

    def test_to_markdown_escapes_special_chars(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.STALE_PR,
            repo_full_name="acme/api",
            description="PR #1: Fix [brackets] and (parens)",
            url="https://example.com",
            age_days=30.0,
            score=50,
        )
        md = item.to_markdown()
        assert "\\[brackets\\]" in md
        assert "\\(parens\\)" in md

    def test_item_id_uses_number_for_prs(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.STALE_PR,
            repo_full_name="acme/api",
            description="PR #42: Some title",
            url="",
            age_days=10,
            score=50,
            number=42,
        )
        assert item.item_id == "stale_pr:acme/api:#42"

    def test_item_id_uses_branch_for_workflows(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.FAILING_WORKFLOW,
            repo_full_name="acme/api",
            description="Workflow 'CI' failing on `main`",
            url="",
            age_days=0,
            score=80,
            branch="main",
        )
        assert item.item_id == "failing_workflow:acme/api:main"

    def test_item_id_falls_back_to_description(self) -> None:
        item = BacklogItem(
            category=BacklogCategory.FAILING_CHECK_ERROR,
            repo_full_name="acme/api",
            description="Check 'lint' failing",
            url="",
            age_days=0,
            score=60,
        )
        assert item.item_id == "failing_check_error:acme/api:Check 'lint' failing"

    def test_item_id_is_stable(self) -> None:
        """Same inputs produce same item_id across calls."""
        item1 = BacklogItem(
            category=BacklogCategory.STALE_ISSUE,
            repo_full_name="org/repo",
            description="Issue #10: Old bug",
            url="",
            age_days=400,
            score=30,
            number=10,
        )
        item2 = BacklogItem(
            category=BacklogCategory.STALE_ISSUE,
            repo_full_name="org/repo",
            description="Issue #10: Old bug",
            url="",
            age_days=401,  # different age shouldn't affect id
            score=31,
            number=10,
        )
        assert item1.item_id == item2.item_id


# ---------------------------------------------------------------------------
# Tests for Markdown export
# ---------------------------------------------------------------------------


class TestExportMarkdown:
    """Tests for the full Markdown export."""

    def test_empty_list(self) -> None:
        md = export_markdown([], title_date="2026-05-01")
        assert "# Grimoire Backlog — 2026-05-01" in md
        assert "## " not in md  # no tier sections

    def test_groups_by_tier(self) -> None:
        items = [
            BacklogItem(
                category=BacklogCategory.FAILING_WORKFLOW,
                repo_full_name="a/b",
                description="wf fail",
                url="",
                age_days=0,
                score=100,
            ),
            BacklogItem(
                category=BacklogCategory.STALE_ISSUE,
                repo_full_name="a/b",
                description="old issue",
                url="",
                age_days=0,
                score=15,
            ),
        ]
        md = export_markdown(items, title_date="2026-05-01")
        assert "## Critical (1 item)" in md
        assert "## Low (1 item)" in md

    def test_items_within_tiers(self) -> None:
        items = [
            BacklogItem(
                category=BacklogCategory.FAILING_WORKFLOW,
                repo_full_name="a/b",
                description="CI fail",
                url="http://x",
                age_days=0,
                score=100,
            ),
            BacklogItem(
                category=BacklogCategory.FAILING_CHECK_ERROR,
                repo_full_name="a/c",
                description="lint fail",
                url="",
                age_days=0,
                score=80,
            ),
        ]
        md = export_markdown(items, title_date="2026-05-01")
        assert "## Critical (2 items)" in md
        assert "**[a/b]**" in md
        assert "**[a/c]**" in md


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------


class TestBacklogRoute:
    """Tests for the /backlog page route."""

    async def test_backlog_page_renders(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog")
        assert resp.status_code == 200
        assert "Backlog" in resp.text

    async def test_backlog_page_shows_failing_workflow(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog")
        assert resp.status_code == 200
        # acme/frontend has a failing Build workflow
        assert "acme/frontend" in resp.text
        assert "Build" in resp.text

    async def test_backlog_page_shows_stale_items(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog")
        assert resp.status_code == 200
        # acme/api has stale PRs and issues
        assert "acme/api" in resp.text

    async def test_backlog_items_partial(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/partials/backlog-items")
        assert resp.status_code == 200
        # Should contain items
        assert "acme/" in resp.text

    async def test_backlog_items_partial_filter_by_category(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?categories=failing_workflow")
        assert resp.status_code == 200
        assert "Build" in resp.text

    async def test_backlog_items_partial_filter_by_repo(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?repos=acme/frontend")
        assert resp.status_code == 200
        assert "acme/frontend" in resp.text
        # Should not show items from acme/api
        assert "acme/api" not in resp.text

    async def test_backlog_items_partial_search_by_repo(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?search=frontend")
        assert resp.status_code == 200
        assert "acme/frontend" in resp.text

    async def test_backlog_items_partial_search_by_description(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?search=Build")
        assert resp.status_code == 200
        assert "Build" in resp.text

    async def test_backlog_items_partial_search_case_insensitive(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?search=build")
        assert resp.status_code == 200
        assert "Build" in resp.text

    async def test_backlog_items_partial_search_no_match(
        self,
        web_client: AsyncClient,
    ) -> None:
        resp = await web_client.get("/partials/backlog-items?search=zzz_no_match_zzz")
        assert resp.status_code == 200
        assert "No items match" in resp.text

    async def test_backlog_export_markdown(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/api/backlog/export")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/markdown")
        assert "# Grimoire Backlog" in resp.text
        assert "- [ ]" in resp.text

    async def test_backlog_page_with_checks(
        self,
        web_client_with_checks: AsyncClient,
    ) -> None:
        resp = await web_client_with_checks.get("/backlog")
        assert resp.status_code == 200
        # Should show the failing watchdog check for acme/frontend
        assert "Watchdog" in resp.text


# ---------------------------------------------------------------------------
# Unit tests for group_by_repo
# ---------------------------------------------------------------------------


class TestGroupByRepo:
    """Tests for the group_by_repo() function."""

    def _make_item(self, repo: str, score: float) -> BacklogItem:
        return BacklogItem(
            category=BacklogCategory.FAILING_WORKFLOW,
            repo_full_name=repo,
            description=f"item in {repo}",
            url="",
            age_days=0.0,
            score=score,
        )

    def test_empty_list(self) -> None:
        assert group_by_repo([]) == []

    def test_single_repo(self) -> None:
        items = [self._make_item("org/a", 50.0), self._make_item("org/a", 30.0)]
        groups = group_by_repo(items)
        assert len(groups) == 1
        assert groups[0].repo_full_name == "org/a"
        assert groups[0].total_score == pytest.approx(80.0)
        assert len(groups[0].items) == 2

    def test_multiple_repos_sorted_by_total_score(self) -> None:
        items = [
            self._make_item("org/low", 10.0),
            self._make_item("org/high", 90.0),
            self._make_item("org/high", 20.0),
            self._make_item("org/mid", 50.0),
        ]
        groups = group_by_repo(items)
        assert len(groups) == 3
        assert groups[0].repo_full_name == "org/high"
        assert groups[0].total_score == pytest.approx(110.0)
        assert groups[1].repo_full_name == "org/mid"
        assert groups[2].repo_full_name == "org/low"

    def test_tier_derived_from_total_score(self) -> None:
        items = [self._make_item("org/crit", 80.0), self._make_item("org/crit", 10.0)]
        groups = group_by_repo(items)
        assert groups[0].tier == "critical"
        assert groups[0].tier_class == "error"

    def test_items_order_preserved_within_group(self) -> None:
        items = [
            self._make_item("org/a", 100.0),
            self._make_item("org/a", 50.0),
            self._make_item("org/a", 10.0),
        ]
        groups = group_by_repo(items)
        scores = [i.score for i in groups[0].items]
        assert scores == [100.0, 50.0, 10.0]


# ---------------------------------------------------------------------------
# Route tests for grouped backlog view
# ---------------------------------------------------------------------------


class TestBacklogGroupedRoutes:
    """Tests for the backlog grouped-by-repo route."""

    async def test_backlog_page_grouped(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog?group_by=repo")
        assert resp.status_code == 200
        # Should contain a <details> element for grouped view
        assert "<details" in resp.text

    async def test_backlog_partial_grouped(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/partials/backlog-items?group_by=repo")
        assert resp.status_code == 200
        assert "<details" in resp.text

    async def test_backlog_partial_flat_default(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/partials/backlog-items")
        assert resp.status_code == 200
        # Flat view should NOT have <details> grouping
        assert "<details" not in resp.text


# ---------------------------------------------------------------------------
# Unit tests for group_by_type
# ---------------------------------------------------------------------------


class TestGroupByType:
    """Tests for the group_by_type() function."""

    def _make_item(
        self,
        category: BacklogCategory,
        repo: str = "org/repo",
        score: float = 50.0,
        description: str = "test item",
    ) -> BacklogItem:
        return BacklogItem(
            category=category,
            repo_full_name=repo,
            description=description,
            url="",
            age_days=0.0,
            score=score,
        )

    def test_empty_list(self) -> None:
        assert group_by_type([]) == []

    def test_stale_issues_grouped(self) -> None:
        items = [
            self._make_item(BacklogCategory.STALE_ISSUE, score=30.0),
            self._make_item(BacklogCategory.STALE_ISSUE, score=20.0),
        ]
        groups = group_by_type(items)
        assert len(groups) == 1
        assert groups[0].key == "stale_issues"
        assert groups[0].label == "Stale Issues"
        assert len(groups[0].items) == 2
        assert groups[0].total_score == pytest.approx(50.0)

    def test_stale_prs_grouped(self) -> None:
        items = [
            self._make_item(BacklogCategory.STALE_PR, score=40.0),
        ]
        groups = group_by_type(items)
        assert len(groups) == 1
        assert groups[0].key == "stale_prs"
        assert groups[0].label == "Stale PRs"

    def test_workflows_grouped_together(self) -> None:
        items = [
            self._make_item(
                BacklogCategory.FAILING_WORKFLOW,
                description="Workflow 'CI' failing on `main`",
                score=100.0,
            ),
            self._make_item(
                BacklogCategory.FAILING_WORKFLOW,
                description="Workflow 'CI' failing on `develop`",
                score=100.0,
            ),
            self._make_item(
                BacklogCategory.FAILING_WORKFLOW,
                description="Workflow 'Deploy' failing on `main`",
                score=80.0,
            ),
        ]
        groups = group_by_type(items)
        assert len(groups) == 1
        assert groups[0].key == "workflows"
        assert groups[0].label == "Workflows"
        assert len(groups[0].items) == 3
        assert groups[0].total_score == pytest.approx(280.0)

    def test_checks_grouped(self) -> None:
        items = [
            self._make_item(BacklogCategory.FAILING_CHECK_ERROR, score=80.0),
            self._make_item(BacklogCategory.FAILING_CHECK_WARNING, score=40.0),
        ]
        groups = group_by_type(items)
        assert len(groups) == 1
        assert groups[0].key == "checks"
        assert groups[0].label == "Checks"
        assert len(groups[0].items) == 2
        assert groups[0].total_score == pytest.approx(120.0)

    def test_ordering_stale_issues_prs_workflows_checks(self) -> None:
        """Groups appear in defined order: stale issues, stale PRs, workflows, checks."""
        items = [
            self._make_item(BacklogCategory.FAILING_CHECK_ERROR),
            self._make_item(
                BacklogCategory.FAILING_WORKFLOW,
                description="Workflow 'CI' failing on `main`",
            ),
            self._make_item(BacklogCategory.STALE_PR),
            self._make_item(BacklogCategory.STALE_ISSUE),
        ]
        groups = group_by_type(items)
        keys = [g.key for g in groups]
        assert keys == ["stale_issues", "stale_prs", "workflows", "checks"]

    def test_tier_derived_from_total_score(self) -> None:
        items = [
            self._make_item(BacklogCategory.STALE_ISSUE, score=80.0),
            self._make_item(BacklogCategory.STALE_ISSUE, score=10.0),
        ]
        groups = group_by_type(items)
        assert groups[0].tier == "critical"
        assert groups[0].tier_class == "error"

    def test_items_order_preserved_within_group(self) -> None:
        items = [
            self._make_item(BacklogCategory.STALE_PR, score=100.0),
            self._make_item(BacklogCategory.STALE_PR, score=50.0),
            self._make_item(BacklogCategory.STALE_PR, score=10.0),
        ]
        groups = group_by_type(items)
        scores = [i.score for i in groups[0].items]
        assert scores == [100.0, 50.0, 10.0]

    def test_mixed_types_all_grouped_correctly(self) -> None:
        items = [
            self._make_item(BacklogCategory.STALE_ISSUE, repo="a/1", score=30.0),
            self._make_item(BacklogCategory.STALE_ISSUE, repo="b/2", score=20.0),
            self._make_item(BacklogCategory.STALE_PR, repo="a/1", score=50.0),
            self._make_item(
                BacklogCategory.FAILING_WORKFLOW,
                repo="a/1",
                description="Workflow 'Build' failing on `main`",
                score=100.0,
            ),
            self._make_item(BacklogCategory.FAILING_CHECK_ERROR, repo="b/2", score=80.0),
        ]
        groups = group_by_type(items)
        assert len(groups) == 4
        assert groups[0].key == "stale_issues"
        assert len(groups[0].items) == 2
        assert groups[1].key == "stale_prs"
        assert len(groups[1].items) == 1
        assert groups[2].key == "workflows"
        assert len(groups[2].items) == 1
        assert groups[3].key == "checks"
        assert len(groups[3].items) == 1


# ---------------------------------------------------------------------------
# Route tests for grouped-by-type backlog view
# ---------------------------------------------------------------------------


class TestBacklogGroupedByTypeRoutes:
    """Tests for the backlog grouped-by-type route."""

    async def test_backlog_page_grouped_by_type(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog?group_by=type")
        assert resp.status_code == 200
        # Should contain a <details> element for grouped view
        assert "<details" in resp.text

    async def test_backlog_partial_grouped_by_type(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/partials/backlog-items?group_by=type")
        assert resp.status_code == 200
        assert "<details" in resp.text

    async def test_backlog_type_view_shows_workflow_group(self, web_client: AsyncClient) -> None:
        resp = await web_client.get("/backlog?group_by=type")
        assert resp.status_code == 200
        # acme/frontend has a failing "Build" workflow, grouped under "Workflows"
        assert "Workflows" in resp.text
