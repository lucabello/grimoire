"""Repository resolution and stats fetching service."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, overload

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from grimoire.config import (
    GrimoireConfig,
    PRGroup,
    StalenessConfig,
    StaticRepoSource,
    TeamRepoSource,
)
from grimoire.database import (
    CachedIssue,
    CachedOpenPullRequest,
    CachedPullRequest,
    CachedRepository,
    CachedWorkflowStatus,
    CheckResultRecord,
)
from grimoire.github.client import ROLLUP_BATCH_SIZE, GitHubClient, NotFoundError, PRRollup
from grimoire.models import (
    IssueDetail,
    OpenPullRequest,
    PullRequestDetail,
    RepositoryStats,
    TrackedRepository,
    WorkflowStatus,
)
from grimoire.prs import CI_WARNING_PREFIX, matches

logger = logging.getLogger(__name__)

_CONCURRENCY_LIMIT = 10


def _brief_error(exc: Exception) -> str:
    """Return a concise, user-friendly description of a fetch error."""
    import httpx

    msg = str(exc)
    if isinstance(exc, httpx.RemoteProtocolError) or "disconnected" in msg.lower():
        return "GitHub disconnected — likely a transient issue. Will retry on next refresh."
    if isinstance(exc, httpx.TimeoutException) or "timeout" in msg.lower():
        return "Request timed out — GitHub may be slow. Will retry on next refresh."
    if "rate limit" in msg.lower():
        return "GitHub API rate limit exceeded. Will resume when the limit resets."
    if isinstance(exc, httpx.ConnectError):
        return "Connection failed — check network connectivity."
    # Generic fallback: truncate long messages
    if len(msg) > 120:
        msg = msg[:117] + "…"
    return msg


# ---------------------------------------------------------------------------
# Refresh progress tracking
# ---------------------------------------------------------------------------


@dataclass
class RefreshProgress:
    """Tracks execution progress of a running data refresh."""

    completed: int = 0
    total: int = 0


_refresh_progress: RefreshProgress | None = None


def is_refresh_running() -> bool:
    """Return True if a data refresh is currently in progress."""
    return _refresh_progress is not None


def get_refresh_progress() -> RefreshProgress | None:
    """Return the progress tracker for a running refresh, or None if idle."""
    return _refresh_progress


def _workflow_matches_filter(wf_name: str, include: list[str], exclude: list[str]) -> bool:
    """Return True if the workflow should be tracked after applying filters.

    * If *include* is non-empty, the name must match at least one include pattern.
    * If *exclude* is non-empty, the name must NOT match any exclude pattern.
    * Both use ``fnmatch`` glob matching (case-sensitive).
    """
    if include and not any(fnmatch(wf_name, pat) for pat in include):
        return False
    if exclude and any(fnmatch(wf_name, pat) for pat in exclude):
        return False
    return True


# ---------------------------------------------------------------------------
# resolve_repositories
# ---------------------------------------------------------------------------


async def resolve_repositories(
    config: GrimoireConfig, client: GitHubClient
) -> list[TrackedRepository]:
    """Build the list of tracked repos from config, validating via the API."""
    seen: dict[str, TrackedRepository] = {}

    for source in config.repositories:
        if isinstance(source, StaticRepoSource):
            await _resolve_static(source, client, seen)
        elif isinstance(source, TeamRepoSource):
            await _resolve_team(source, client, seen)

    return list(seen.values())


async def _resolve_static(
    source: StaticRepoSource,
    client: GitHubClient,
    seen: dict[str, TrackedRepository],
) -> None:
    try:
        data = await client.get_repo(source.repo)
        if data is None:
            # 304 cache hit — use the repo name as-is
            default_branch = "main"
            archived = False
        else:
            default_branch = data.get("default_branch", "main")
            archived = data.get("archived", False)

        if archived:
            logger.info("Skipping archived repository %s", source.repo)
            return

        branches = source.branches if source.branches else [default_branch]

        if source.repo in seen:
            existing = seen[source.repo]
            merged = list(dict.fromkeys(existing.branches + branches))
            seen[source.repo] = existing.model_copy(update={"branches": merged})
        else:
            seen[source.repo] = TrackedRepository(
                full_name=source.repo,
                default_branch=default_branch,
                branches=branches,
                source="static",
                workflow_include=source.workflows.include,
                workflow_exclude=source.workflows.exclude,
            )
    except Exception:
        logger.warning("Failed to resolve static repo %s, skipping", source.repo, exc_info=True)


async def _resolve_team(
    source: TeamRepoSource,
    client: GitHubClient,
    seen: dict[str, TrackedRepository],
) -> None:
    # team format: "org/team-slug"
    try:
        org, team_slug = source.team.split("/", 1)
    except ValueError:
        logger.warning("Invalid team format %r, expected 'org/team-slug'", source.team)
        return

    try:
        repos = await client.get_team_repos(org, team_slug)
    except Exception:
        logger.warning("Failed to fetch repos for team %s, skipping", source.team, exc_info=True)
        return

    exclude_set = set(source.exclude)

    for repo_data in repos:
        full_name = repo_data.get("full_name", "")
        if full_name in exclude_set:
            continue
        if repo_data.get("archived", False):
            continue

        default_branch = repo_data.get("default_branch", "main")
        branches = [default_branch]
        source_label = f"team:{source.team}"

        if full_name in seen:
            existing = seen[full_name]
            merged = list(dict.fromkeys(existing.branches + branches))
            seen[full_name] = existing.model_copy(update={"branches": merged})
        else:
            seen[full_name] = TrackedRepository(
                full_name=full_name,
                default_branch=default_branch,
                branches=branches,
                source=source_label,
                workflow_include=source.workflows.include,
                workflow_exclude=source.workflows.exclude,
            )


# ---------------------------------------------------------------------------
# fetch_repository_stats
# ---------------------------------------------------------------------------


async def fetch_repository_stats(
    repo: TrackedRepository,
    client: GitHubClient,
    staleness: StalenessConfig,
    previous: RepositoryStats | None = None,
) -> RepositoryStats:
    """Fetch issues, PRs, and workflow statuses for a single repository.

    When the API returns 304 (Not Modified), *previous* values are preserved
    instead of being reset to zero.
    """
    warnings: list[str] = []
    now = datetime.now(UTC)

    # -- Issues --------------------------------------------------------------
    open_issues = previous.open_issues if previous else 0
    stale_issues = previous.stale_issues if previous else 0
    stale_issue_items: list[IssueDetail] = list(previous.stale_issue_items) if previous else []
    oldest_issue_created_at: datetime | None = (
        previous.oldest_issue_created_at if previous else None
    )
    issue_created_dates: list[datetime] = list(previous.issue_created_dates) if previous else []
    try:
        issues = await client.get_open_issues(repo.full_name)
        if issues is not None:
            open_issues = len(issues)
            stale_issues = 0
            stale_issue_items = []
            issue_created_dates = []
            stale_cutoff = now - timedelta(days=staleness.issues_days)
            for issue in issues:
                created_at = _parse_dt(issue.get("created_at"))
                if created_at is not None:
                    issue_created_dates.append(created_at)
                last_activity = _parse_dt(issue.get("updated_at")) or created_at
                if _is_issue_stale(issue, stale_cutoff):
                    issue_number = issue.get("number")
                    if not issue_number or issue_number <= 0:
                        continue
                    stale_issues += 1
                    stale_issue_items.append(
                        IssueDetail(
                            number=issue_number,
                            title=issue.get("title", ""),
                            url=issue.get("html_url", ""),
                            created_at=created_at or now,
                            last_activity_at=last_activity,
                            author=issue.get("user", {}).get("login", ""),
                        )
                    )
            oldest_issue_created_at = min(issue_created_dates) if issue_created_dates else None
    except Exception as exc:
        warnings.append(f"Could not fetch issues for {repo.full_name}: {_brief_error(exc)}")

    # -- Pull Requests -------------------------------------------------------
    open_prs = previous.open_pull_requests if previous else 0
    stale_prs = previous.stale_pull_requests if previous else 0
    stale_pr_items: list[PullRequestDetail] = list(previous.stale_pr_items) if previous else []
    oldest_pr_created_at: datetime | None = previous.oldest_pr_created_at if previous else None
    pr_created_dates: list[datetime] = list(previous.pr_created_dates) if previous else []
    open_pr_items: list[OpenPullRequest] = list(previous.open_pr_items) if previous else []
    try:
        prs = await client.get_open_pull_requests(repo.full_name)
        if prs is not None:
            open_prs = len(prs)
            stale_prs = 0
            stale_pr_items = []
            open_pr_items = []
            pr_created_dates = []
            stale_cutoff = now - timedelta(days=staleness.pull_requests_days)
            for pr in prs:
                created_at = _parse_dt(pr.get("created_at"))
                if created_at is not None:
                    pr_created_dates.append(created_at)
                last_activity = _parse_dt(pr.get("updated_at")) or created_at
                open_pr = _to_open_pull_request(pr, created_at or now)
                if open_pr is not None:
                    open_pr_items.append(open_pr)
                if _is_pr_stale(pr, stale_cutoff):
                    pr_number = pr.get("number")
                    if not pr_number or pr_number <= 0:
                        continue
                    stale_prs += 1
                    stale_pr_items.append(
                        PullRequestDetail(
                            number=pr_number,
                            title=pr.get("title", ""),
                            url=pr.get("html_url", ""),
                            created_at=created_at or now,
                            last_activity_at=last_activity,
                            author=pr.get("user", {}).get("login", ""),
                        )
                    )
            oldest_pr_created_at = min(pr_created_dates) if pr_created_dates else None
    except Exception as exc:
        warnings.append(f"Could not fetch PRs for {repo.full_name}: {_brief_error(exc)}")

    # -- Workflows -----------------------------------------------------------
    workflow_statuses: list[WorkflowStatus] = list(previous.workflows) if previous else []
    # Build lookup of previous statuses so we can fall back on 304 cache hits
    prev_wf_map: dict[tuple[str, str], WorkflowStatus] = {}
    if previous:
        for pw in previous.workflows:
            prev_wf_map[(pw.name, pw.branch)] = pw
    try:
        workflows = await client.get_workflows(repo.full_name)
        if workflows is not None:
            workflow_statuses = []  # fresh data, reset
            for branch in repo.branches:
                for wf in workflows:
                    wf_id = wf.get("id")
                    wf_name = wf.get("name", "unknown")
                    if wf_id is None:
                        continue
                    if not _workflow_matches_filter(
                        wf_name, repo.workflow_include, repo.workflow_exclude
                    ):
                        continue
                    try:
                        runs = await client.get_workflow_runs(repo.full_name, wf_id, branch)
                        if runs is not None and branch == repo.default_branch:
                            # Workflows triggered by non-branch events (e.g. `release`,
                            # tag pushes) report head_branch as the tag, never the
                            # tracked branch — so a branch-filtered run can exist but
                            # be stale (e.g. an old failed run) while newer runs are
                            # invisible to the filter. For the default branch only,
                            # compare against the latest run overall and keep whichever
                            # is more recent. Non-default branches never get this
                            # fallback: periodic/release-only workflows should not be
                            # shown as if they ran on branches that never triggered them.
                            #
                            # Runs whose head_branch is another *tracked* branch (e.g.
                            # a release run actually triggered from `track/3.0`) must
                            # be excluded here — otherwise they'd be misattributed to
                            # the default branch just because they're the newest.
                            other_branches = {b for b in repo.branches if b != branch}
                            unscoped_runs = await client.get_workflow_runs(
                                repo.full_name, wf_id, per_page=10
                            )
                            if unscoped_runs:
                                candidates = [
                                    r
                                    for r in unscoped_runs
                                    if r.get("head_branch") not in other_branches
                                ]
                                latest_unscoped = candidates[0] if candidates else None
                                latest_branch = runs[0] if runs else None
                                if latest_unscoped is not None and (
                                    latest_branch is None
                                    or _run_created_at(latest_unscoped)
                                    > _run_created_at(latest_branch)
                                ):
                                    runs = [latest_unscoped]
                        if runs is not None and runs:
                            run = runs[0]
                            conclusion = run.get("conclusion") or "pending"
                            status_str = _map_conclusion(conclusion)
                            workflow_statuses.append(
                                WorkflowStatus(
                                    name=wf_name,
                                    branch=branch,
                                    status=status_str,
                                    url=wf.get("html_url", ""),
                                    run_url=run.get("html_url", ""),
                                    run_id=run.get("id"),
                                    run_attempt=run.get("run_attempt"),
                                    event=run.get("event"),
                                    head_sha=run.get("head_sha"),
                                    conclusion=run.get("conclusion"),
                                    created_at=_parse_dt(run.get("created_at")),
                                    run_started_at=_parse_dt(run.get("run_started_at")),
                                    updated_at=_parse_dt(run.get("updated_at")),
                                )
                            )
                        elif runs is None:
                            # 304 cache hit — preserve previous status if available
                            prev = prev_wf_map.get((wf_name, branch))
                            if prev is not None:
                                workflow_statuses.append(prev)
                    except NotFoundError:
                        pass  # Non-existing branches from config are silently ignored
                    except Exception as exc:
                        warnings.append(
                            f"Could not fetch runs for workflow '{wf_name}' on {branch}: {_brief_error(exc)}"
                        )
    except Exception as exc:
        warnings.append(f"Could not fetch workflows for {repo.full_name}: {_brief_error(exc)}")

    # -- Branches & last commit ----------------------------------------------
    last_commit_at: datetime | None = previous.last_commit_at if previous else None
    total_branches: int = previous.total_branches if previous else 0

    # Fetch last commit time for each observed branch
    branch_commit_dates: list[datetime] = []
    for branch in repo.branches:
        try:
            branch_data = await client.get_branch(repo.full_name, branch)
            if branch_data is not None:
                commit_date_str = (
                    branch_data.get("commit", {})
                    .get("commit", {})
                    .get("committer", {})
                    .get("date")
                )
                dt = _parse_dt(commit_date_str)
                if dt is not None:
                    branch_commit_dates.append(dt)
        except NotFoundError:
            pass  # Non-existing branches from config are silently ignored
        except Exception as exc:
            warnings.append(f"Could not fetch branch '{branch}' info: {_brief_error(exc)}")

    if branch_commit_dates:
        last_commit_at = max(branch_commit_dates)

    # Fetch all branches for total count
    try:
        all_branches = await client.get_branches(repo.full_name)
        if all_branches is not None:
            total_branches = len(all_branches)
    except Exception as exc:
        warnings.append(f"Could not fetch branches for {repo.full_name}: {_brief_error(exc)}")

    return RepositoryStats(
        full_name=repo.full_name,
        default_branch=repo.default_branch,
        open_issues=open_issues,
        stale_issues=stale_issues,
        open_pull_requests=open_prs,
        stale_pull_requests=stale_prs,
        workflows=workflow_statuses,
        stale_issue_items=stale_issue_items,
        stale_pr_items=stale_pr_items,
        open_pr_items=open_pr_items,
        warnings=warnings,
        fetched_at=now,
        last_commit_at=last_commit_at,
        total_branches=total_branches,
        oldest_issue_created_at=oldest_issue_created_at,
        oldest_pr_created_at=oldest_pr_created_at,
        issue_created_dates=issue_created_dates,
        pr_created_dates=pr_created_dates,
    )


def _to_open_pull_request(pr: dict[str, Any], created_at: datetime) -> OpenPullRequest | None:
    """Map a pulls-list entry; entries without a valid number are skipped."""
    number = pr.get("number")
    if not number or number <= 0:
        return None
    return OpenPullRequest(
        number=number,
        title=pr.get("title", ""),
        url=pr.get("html_url", ""),
        author=(pr.get("user") or {}).get("login", ""),
        created_at=created_at,
        updated_at=_parse_dt(pr.get("updated_at")),
        draft=bool(pr.get("draft")),
        labels=[label["name"] for label in pr.get("labels", [])],
        base_ref=(pr.get("base") or {}).get("ref", ""),
        head_ref=(pr.get("head") or {}).get("ref", ""),
        head_sha=(pr.get("head") or {}).get("sha", ""),
        auto_merge=pr.get("auto_merge") is not None,
    )


def _map_conclusion(conclusion: str) -> str:
    mapping = {
        "success": "success",
        "failure": "failure",
        "cancelled": "failure",
        "timed_out": "failure",
        "action_required": "pending",
        "pending": "pending",
        "queued": "pending",
        "in_progress": "pending",
    }
    return mapping.get(conclusion, "unknown")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


@overload
def _as_utc(value: datetime) -> datetime: ...
@overload
def _as_utc(value: None) -> None: ...
def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; restore UTC so cached values serialise like fresh ones."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _run_created_at(run: dict[str, Any]) -> datetime:
    """Return a workflow run's creation time, or epoch if missing/invalid."""
    return _parse_dt(run.get("created_at")) or datetime.min.replace(tzinfo=UTC)


def _is_issue_stale(issue: dict[str, Any], cutoff: datetime) -> bool:
    """An issue is stale if its last comment (or creation) is before *cutoff*."""
    last_comment = _parse_dt(issue.get("updated_at"))
    created = _parse_dt(issue.get("created_at"))
    reference = last_comment or created
    if reference is None:
        return True
    return reference < cutoff


def _is_pr_stale(pr: dict[str, Any], cutoff: datetime) -> bool:
    """A PR is stale if max(last push, last comment) is before *cutoff*."""
    updated = _parse_dt(pr.get("updated_at"))
    created = _parse_dt(pr.get("created_at"))
    reference = updated or created
    if reference is None:
        return True
    return reference < cutoff


# ---------------------------------------------------------------------------
# refresh_all_stats
# ---------------------------------------------------------------------------


async def refresh_all_stats(
    config: GrimoireConfig, client: GitHubClient
) -> tuple[list[TrackedRepository], list[RepositoryStats]]:
    """Resolve repos, fetch stats concurrently, persist to DB, return results."""
    global _refresh_progress  # noqa: PLW0603

    # Compute a preliminary repo count from config so the progress bar can
    # show a meaningful denominator before any async work completes.
    # Static repos are known exactly; team repos need API resolution, so we
    # fall back to the cached count if available.
    preliminary_total = sum(1 for src in config.repositories if isinstance(src, StaticRepoSource))

    # Mark refresh as running immediately (before async work begins) so that
    # polling endpoints see it as in-progress right away.
    progress = RefreshProgress(completed=0, total=preliminary_total)
    _refresh_progress = progress

    try:
        # Load previous data so 304 (Not Modified) responses preserve old values
        cached_repos, old_stats_list = await load_stats_from_db(client._engine)
        old_stats_map = {s.full_name: s for s in old_stats_list}

        # Refine the estimate with the cached count (covers team repos).
        if cached_repos and len(cached_repos) > progress.total:
            progress.total = len(cached_repos)

        repos = await resolve_repositories(config, client)

        # When the GitHub API returns 304 for repo resolution (ETag cache hit),
        # resolve_repositories returns an empty list because the paginated response
        # is None.  Fall back to the DB-cached repo list so we don't lose data.
        if not repos and cached_repos:
            logger.info(
                "Repo resolution returned no results (likely 304 cache hit), reusing %d cached repos",
                len(cached_repos),
            )
            repos = cached_repos

        progress.total = len(repos)

        sem = asyncio.Semaphore(_CONCURRENCY_LIMIT)

        async def _fetch(repo: TrackedRepository) -> RepositoryStats:
            async with sem:
                result = await fetch_repository_stats(
                    repo,
                    client,
                    config.staleness,
                    previous=old_stats_map.get(repo.full_name),
                )
                progress.completed += 1
                return result

        stats = await asyncio.gather(*[_fetch(r) for r in repos])
        stats_list = list(stats)

        await refresh_pull_request_ci(client, config.prs.groups, stats_list, old_stats_map)

        await save_stats_to_db(
            client._engine,
            stats_list,
            repos,
        )
        return repos, stats_list
    finally:
        _refresh_progress = None


async def refresh_pull_request_ci(
    client: GitHubClient,
    groups: list[PRGroup],
    stats_list: list[RepositoryStats],
    previous: dict[str, RepositoryStats],
) -> None:
    """Fill CI, review and merge state of the pull requests that match a group.

    Pull requests are queried in sequential GraphQL batches. A pull request GitHub did not
    report (failed batch, or not found) keeps the state it had in *previous* when its head
    commit is unchanged, and has none otherwise. A failed batch adds a warning to each of
    its repositories.
    """
    if not groups:
        return

    targets: list[tuple[str, int]] = [
        (stats.full_name, pr.number)
        for stats in stats_list
        for pr in stats.open_pr_items
        if any(matches(group, stats.full_name, pr) for group in groups)
    ]
    stats_by_repo = {stats.full_name: stats for stats in stats_list}

    rollups: dict[tuple[str, int], PRRollup] = {}
    for start in range(0, len(targets), ROLLUP_BATCH_SIZE):
        batch = targets[start : start + ROLLUP_BATCH_SIZE]
        try:
            rollups.update(await client.get_pull_request_rollups(batch))
        except Exception as exc:
            for full_name in dict.fromkeys(name for name, _ in batch):
                stats_by_repo[full_name].warnings.append(
                    f"{CI_WARNING_PREFIX} for {full_name}: {_brief_error(exc)}"
                )

    previous_items = {(name, p.number): p for name, s in previous.items() for p in s.open_pr_items}
    wanted = set(targets)
    now = datetime.now(UTC)
    for stats in stats_list:
        updated: list[OpenPullRequest] = []
        for pr in stats.open_pr_items:
            key = (stats.full_name, pr.number)
            if key in wanted:
                rollup = rollups.get(key)
                old = previous_items.get(key)
                if rollup is not None:
                    pr = pr.model_copy(
                        update={
                            "ci_state": rollup.ci_state,
                            "review_decision": rollup.review_decision,
                            "merge_state": rollup.merge_state,
                            "ci_fetched_at": now,
                        }
                    )
                elif old is not None and old.head_sha == pr.head_sha:
                    pr = pr.model_copy(
                        update={
                            "ci_state": old.ci_state,
                            "review_decision": old.review_decision,
                            "merge_state": old.merge_state,
                            "ci_fetched_at": old.ci_fetched_at,
                        }
                    )
            updated.append(pr)
        stats.open_pr_items = updated


# ---------------------------------------------------------------------------
# DB persistence helpers
# ---------------------------------------------------------------------------


async def save_stats_to_db(
    engine: AsyncEngine,
    stats_list: list[RepositoryStats],
    repos: list[TrackedRepository],
) -> None:
    """Clear old cached data for each repo and write fresh records."""
    now = datetime.now(UTC)
    repo_map = {r.full_name: r for r in repos}

    async with AsyncSession(engine) as session:
        for stats in stats_list:
            repo = repo_map.get(stats.full_name)
            if repo is None:
                continue

            # Delete old cached data for this repo
            fn = stats.full_name
            await session.exec(  # type: ignore[call-overload]
                delete(CachedIssue).where(CachedIssue.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedPullRequest).where(
                    CachedPullRequest.repo_full_name == fn  # type: ignore[arg-type]
                )
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedOpenPullRequest).where(
                    CachedOpenPullRequest.repo_full_name == fn  # type: ignore[arg-type]
                )
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedWorkflowStatus).where(
                    CachedWorkflowStatus.repo_full_name == fn  # type: ignore[arg-type]
                )
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedRepository).where(
                    CachedRepository.full_name == fn  # type: ignore[arg-type]
                )
            )

            # Write CachedRepository
            session.add(
                CachedRepository(
                    full_name=stats.full_name,
                    default_branch=stats.default_branch,
                    source=repo.source,
                    branches_json=json.dumps(repo.branches),
                    open_issues=stats.open_issues,
                    stale_issues=stats.stale_issues,
                    open_pull_requests=stats.open_pull_requests,
                    stale_pull_requests=stats.stale_pull_requests,
                    last_commit_at=stats.last_commit_at,
                    total_branches=stats.total_branches,
                    workflow_include_json=json.dumps(repo.workflow_include),
                    workflow_exclude_json=json.dumps(repo.workflow_exclude),
                    fetched_at=stats.fetched_at or now,
                )
            )

            # Write CachedWorkflowStatus
            for wf in stats.workflows:
                session.add(
                    CachedWorkflowStatus(
                        repo_full_name=stats.full_name,
                        workflow_name=wf.name,
                        branch=wf.branch,
                        status=wf.status,
                        url=wf.url,
                        run_url=wf.run_url,
                        run_id=wf.run_id,
                        run_attempt=wf.run_attempt,
                        event=wf.event,
                        head_sha=wf.head_sha,
                        conclusion=wf.conclusion,
                        run_created_at=wf.created_at,
                        run_started_at=wf.run_started_at,
                        run_updated_at=wf.updated_at,
                        fetched_at=stats.fetched_at or now,
                    )
                )

            # Write stale issues
            for item in stats.stale_issue_items:
                session.add(
                    CachedIssue(
                        repo_full_name=stats.full_name,
                        title=item.title,
                        number=item.number,
                        url=item.url,
                        author=item.author,
                        created_at=item.created_at,
                        last_comment_at=item.last_activity_at,
                        fetched_at=stats.fetched_at or now,
                    )
                )

            # Write stale PRs
            for item in stats.stale_pr_items:
                session.add(
                    CachedPullRequest(
                        repo_full_name=stats.full_name,
                        title=item.title,
                        number=item.number,
                        url=item.url,
                        author=item.author,
                        created_at=item.created_at,
                        last_comment_at=item.last_activity_at,
                        fetched_at=stats.fetched_at or now,
                    )
                )

            # Write open PRs
            for open_pr in stats.open_pr_items:
                session.add(
                    CachedOpenPullRequest(
                        repo_full_name=stats.full_name,
                        number=open_pr.number,
                        title=open_pr.title,
                        url=open_pr.url,
                        author=open_pr.author,
                        created_at=open_pr.created_at,
                        updated_at=open_pr.updated_at,
                        draft=open_pr.draft,
                        labels_json=json.dumps(open_pr.labels),
                        base_ref=open_pr.base_ref,
                        head_ref=open_pr.head_ref,
                        head_sha=open_pr.head_sha,
                        auto_merge=open_pr.auto_merge,
                        ci_state=open_pr.ci_state,
                        review_decision=open_pr.review_decision,
                        merge_state=open_pr.merge_state,
                        ci_fetched_at=open_pr.ci_fetched_at,
                        fetched_at=stats.fetched_at or now,
                    )
                )

        await session.commit()


async def load_stats_from_db(
    engine: AsyncEngine,
) -> tuple[list[TrackedRepository], list[RepositoryStats]]:
    """Load all cached data from the DB."""
    repos: list[TrackedRepository] = []
    stats_list: list[RepositoryStats] = []

    async with AsyncSession(engine) as session:
        cached_repos = (await session.exec(select(CachedRepository))).all()

        for cr in cached_repos:
            branches = json.loads(cr.branches_json) if cr.branches_json else []
            repo = TrackedRepository(
                full_name=cr.full_name,
                default_branch=cr.default_branch,
                branches=branches,
                source=cr.source,
                workflow_include=json.loads(cr.workflow_include_json)
                if cr.workflow_include_json
                else [],
                workflow_exclude=json.loads(cr.workflow_exclude_json)
                if cr.workflow_exclude_json
                else [],
            )
            repos.append(repo)

            # Load workflow statuses
            wf_rows = (
                await session.exec(
                    select(CachedWorkflowStatus).where(
                        CachedWorkflowStatus.repo_full_name == cr.full_name
                    )
                )
            ).all()
            workflows = [
                WorkflowStatus(
                    name=w.workflow_name,
                    branch=w.branch,
                    status=w.status,
                    url=w.url,
                    run_url=w.run_url,
                    run_id=w.run_id,
                    run_attempt=w.run_attempt,
                    event=w.event,
                    head_sha=w.head_sha,
                    conclusion=w.conclusion,
                    created_at=_as_utc(w.run_created_at),
                    run_started_at=_as_utc(w.run_started_at),
                    updated_at=_as_utc(w.run_updated_at),
                )
                for w in wf_rows
            ]

            # Load stale issues from cache
            issue_rows = (
                await session.exec(
                    select(CachedIssue).where(CachedIssue.repo_full_name == cr.full_name)
                )
            ).all()
            stale_issue_items = [
                IssueDetail(
                    number=i.number,
                    title=i.title,
                    url=i.url,
                    created_at=_as_utc(i.created_at),
                    last_activity_at=_as_utc(i.last_comment_at),
                    author=i.author,
                )
                for i in issue_rows
            ]

            # Load stale PRs from cache
            pr_rows = (
                await session.exec(
                    select(CachedPullRequest).where(
                        CachedPullRequest.repo_full_name == cr.full_name
                    )
                )
            ).all()
            stale_pr_items = [
                PullRequestDetail(
                    number=p.number,
                    title=p.title,
                    url=p.url,
                    created_at=_as_utc(p.created_at),
                    last_activity_at=_as_utc(p.last_comment_at),
                    author=p.author,
                )
                for p in pr_rows
            ]

            # Load open PRs from cache
            open_pr_rows = (
                await session.exec(
                    select(CachedOpenPullRequest)
                    .where(CachedOpenPullRequest.repo_full_name == cr.full_name)
                    .order_by(CachedOpenPullRequest.id)  # type: ignore[arg-type]
                )
            ).all()
            open_pr_items = [
                OpenPullRequest(
                    number=p.number,
                    title=p.title,
                    url=p.url,
                    author=p.author,
                    created_at=_as_utc(p.created_at),
                    updated_at=_as_utc(p.updated_at),
                    draft=p.draft,
                    labels=json.loads(p.labels_json),
                    base_ref=p.base_ref,
                    head_ref=p.head_ref,
                    head_sha=p.head_sha,
                    auto_merge=p.auto_merge,
                    ci_state=p.ci_state,
                    review_decision=p.review_decision,
                    merge_state=p.merge_state,
                    ci_fetched_at=_as_utc(p.ci_fetched_at),
                )
                for p in open_pr_rows
            ]

            stats_list.append(
                RepositoryStats(
                    full_name=cr.full_name,
                    default_branch=cr.default_branch,
                    open_issues=cr.open_issues,
                    stale_issues=cr.stale_issues or len(issue_rows),
                    open_pull_requests=cr.open_pull_requests,
                    stale_pull_requests=cr.stale_pull_requests or len(pr_rows),
                    workflows=workflows,
                    stale_issue_items=stale_issue_items,
                    stale_pr_items=stale_pr_items,
                    open_pr_items=open_pr_items,
                    fetched_at=_as_utc(cr.fetched_at),
                    last_commit_at=_as_utc(cr.last_commit_at),
                    total_branches=cr.total_branches,
                )
            )

    return repos, stats_list


async def prune_removed_repos(engine: AsyncEngine, config: GrimoireConfig) -> int:
    """Delete DB-cached data for repos no longer present in the config.

    Only performs pruning when the config contains exclusively static repo
    sources (``repo:`` entries) so the full set of expected names is known
    without making API calls.  Returns the number of repos pruned.
    """
    has_team_sources = any(isinstance(s, TeamRepoSource) for s in config.repositories)
    if has_team_sources:
        logger.debug("Config contains team sources — skipping DB prune")
        return 0

    configured_names: set[str] = set()
    for source in config.repositories:
        if isinstance(source, StaticRepoSource):
            configured_names.add(source.repo)

    if not configured_names:
        return 0

    return await _prune_repos_from_db(engine, configured_names)


async def prune_stale_data(
    engine: AsyncEngine,
    repos: list[TrackedRepository],
    workspace_dir: Path | None = None,
) -> int:
    """Remove DB and disk data for repos/workflows no longer in the resolved set.

    Unlike ``prune_removed_repos``, this works after resolution (including
    team sources) because the caller provides the authoritative repo list.
    Also cleans up excluded workflow statuses from the DB and removes
    workspace directories for pruned repos.

    Returns the number of repos pruned.
    """
    active_names = {r.full_name for r in repos}
    pruned = await _prune_repos_from_db(engine, active_names)

    # Clean up excluded workflows: for each active repo, delete cached
    # workflow statuses that no longer pass the include/exclude filter.
    async with AsyncSession(engine) as session:
        for repo in repos:
            if not repo.workflow_include and not repo.workflow_exclude:
                continue
            wf_rows = (
                await session.exec(
                    select(CachedWorkflowStatus).where(
                        CachedWorkflowStatus.repo_full_name == repo.full_name
                    )
                )
            ).all()
            for wf in wf_rows:
                if not _workflow_matches_filter(
                    wf.workflow_name, repo.workflow_include, repo.workflow_exclude
                ):
                    await session.delete(wf)
        await session.commit()

    # Clean up workspace directories for pruned repos
    if workspace_dir and pruned:
        _prune_workspace_dirs(workspace_dir, active_names)

    return pruned


async def _prune_repos_from_db(engine: AsyncEngine, active_names: set[str]) -> int:
    """Delete DB rows for repos not in *active_names*.  Returns count pruned."""
    pruned = 0
    async with AsyncSession(engine) as session:
        cached = (await session.exec(select(CachedRepository.full_name))).all()
        stale_names = [name for name in cached if name not in active_names]

        for fn in stale_names:
            await session.exec(  # type: ignore[call-overload]
                delete(CachedIssue).where(CachedIssue.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedPullRequest).where(CachedPullRequest.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedOpenPullRequest).where(CachedOpenPullRequest.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedWorkflowStatus).where(CachedWorkflowStatus.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CheckResultRecord).where(CheckResultRecord.repo_full_name == fn)  # type: ignore[arg-type]
            )
            await session.exec(  # type: ignore[call-overload]
                delete(CachedRepository).where(CachedRepository.full_name == fn)  # type: ignore[arg-type]
            )
            pruned += 1

        if pruned:
            await session.commit()
            logger.info("Pruned %d repos no longer tracked: %s", pruned, stale_names)

    return pruned


def _prune_workspace_dirs(workspace_dir: Path, active_names: set[str]) -> None:
    """Remove workspace directories for repos no longer tracked."""
    import shutil

    active_owners: dict[str, set[str]] = {}
    for name in active_names:
        owner, repo = name.split("/", 1)
        active_owners.setdefault(owner, set()).add(repo)

    if not workspace_dir.is_dir():
        return

    for owner_dir in workspace_dir.iterdir():
        if not owner_dir.is_dir() or owner_dir.name.startswith("."):
            continue
        for repo_dir in owner_dir.iterdir():
            if not repo_dir.is_dir() or repo_dir.name.startswith("."):
                continue
            full_name = f"{owner_dir.name}/{repo_dir.name}"
            if full_name not in active_names:
                logger.info("Removing workspace for pruned repo: %s", full_name)
                shutil.rmtree(repo_dir, ignore_errors=True)
        # Remove empty owner directories
        if owner_dir.is_dir() and not any(owner_dir.iterdir()):
            owner_dir.rmdir()
