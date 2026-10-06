"""FastAPI application factory for Grimoire."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import AsyncIterator

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from grimoire.actions.loader import load_actions
from grimoire.actions.router import router as actions_router
from grimoire.actions.router import set_actions_state
from grimoire.actions.scheduler import register_actions
from grimoire.checks.engine import run_check_for_all_targets
from grimoire.checks.loader import load_checks
from grimoire.checks.router import router as checks_router
from grimoire.checks.router import set_checks_state
from grimoire.checks.scheduler import register_checks
from grimoire.config import load_config
from grimoire.database import (
    create_tables,
    get_engine,
    restore_action_toggles,
    restore_check_toggles,
)
from grimoire.github.client import GitHubClient
from grimoire.github.router import (
    _cache,
    _last_refresh,
    refresh_router,
    set_refresh_callback,
    update_cache,
)
from grimoire.github.router import (
    router as repos_router,
)
from grimoire.github.service import (
    load_stats_from_db,
    prune_removed_repos,
    prune_stale_data,
    refresh_all_stats,
)
from grimoire.models import TrackedRepository
from grimoire.observability.logging import setup_logging
from grimoire.observability.metrics import DATA_REFRESH_DURATION, update_repo_metrics
from grimoire.observability.metrics import router as metrics_router
from grimoire.web.router import router as web_router
from grimoire.web.router import (
    set_backlog_config,
    set_configured_teams,
    set_refresh_schedule,
    set_staleness_config,
)
from grimoire.workspace.manager import WorkspaceManager

logger = logging.getLogger(__name__)

VERSION = version("grimoire-dashboard")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Application startup and shutdown lifecycle."""
    setup_logging()
    logger.info("Grimoire starting up")

    scheduler: AsyncIOScheduler | None = None
    client: GitHubClient | None = None

    try:
        config = load_config()
    except FileNotFoundError as exc:
        logger.warning("Config not loaded — running in empty mode: %s", exc)
        yield
        return

    # Resolve the config file path for save-weights API
    import os

    _env_path = os.environ.get("GRIMOIRE_CONFIG")
    config_file_path = Path(_env_path) if _env_path else Path("config.yaml")

    # Ensure data directories exist
    config.database_path.parent.mkdir(parents=True, exist_ok=True)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.workspace_dir.mkdir(parents=True, exist_ok=True)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    # Database
    engine = await get_engine(str(config.database_path))
    await create_tables(engine)

    # Clean up any run records left in 'running' state from a previous crash
    from grimoire.database import cleanup_stale_runs

    await cleanup_stale_runs(engine)

    # Expose staleness thresholds to web layer
    set_staleness_config(config.staleness)

    # Expose backlog config to web layer
    set_backlog_config(config.backlog, config_file_path)

    # Expose refresh schedule to web layer
    set_refresh_schedule(config.refresh_schedule)

    # Expose configured team sources for the dashboard filter dropdown
    from grimoire.config import TeamRepoSource

    set_configured_teams([s.team for s in config.repositories if isinstance(s, TeamRepoSource)])

    # Prune DB-cached repos that are no longer in the config
    await prune_removed_repos(engine, config)

    # GitHub client
    client = GitHubClient(config.github.token, engine)

    # Load data — prefer DB cache if fresh enough, otherwise do API refresh
    repos: list[TrackedRepository] = []
    need_background_refresh = False
    try:
        cached_repos, cached_stats = await load_stats_from_db(engine)
        cache_is_fresh = False
        newest_fetched_at: datetime | None = None
        if cached_repos:
            oldest = min(
                (s.fetched_at for s in cached_stats if s.fetched_at),
                default=None,
            )
            # The newest fetched_at represents when the last refresh completed
            newest_fetched_at = max(
                (s.fetched_at for s in cached_stats if s.fetched_at),
                default=None,
            )
            if oldest:
                age_minutes = (datetime.now(tz=timezone.utc) - oldest).total_seconds() / 60
                # Compute expected interval from the cron schedule
                trigger = CronTrigger.from_crontab(config.refresh_schedule)
                now = datetime.now(tz=timezone.utc)
                next1 = trigger.get_next_fire_time(None, now)
                next2 = trigger.get_next_fire_time(next1, next1) if next1 else None
                interval_minutes = (next2 - next1).total_seconds() / 60 if next1 and next2 else 5
                cache_is_fresh = age_minutes < interval_minutes
                logger.info(
                    "DB cache: %d repos, oldest data %.1f min ago (%s)",
                    len(cached_repos),
                    age_minutes,
                    "fresh" if cache_is_fresh else "stale",
                )

        if cached_repos and cache_is_fresh:
            update_cache(cached_repos, cached_stats, timestamp=newest_fetched_at)
            update_repo_metrics(cached_stats)
            repos = cached_repos
            logger.info("Using cached data — %d repositories loaded", len(repos))
        elif cached_repos:
            # Stale cache — serve it immediately, refresh in background
            update_cache(cached_repos, cached_stats, timestamp=newest_fetched_at)
            update_repo_metrics(cached_stats)
            repos = cached_repos
            need_background_refresh = True
            logger.info(
                "Serving stale cache (%d repos) — background refresh scheduled",
                len(repos),
            )
        else:
            # No cache at all — start server immediately, refresh in background
            need_background_refresh = True
            logger.info("No cached data — background refresh scheduled")
    except Exception:
        logger.exception("Data loading failed — falling back to cached data")
        try:
            cached_repos, cached_stats = await load_stats_from_db(engine)
            if cached_repos:
                newest_fetched_at = max(
                    (s.fetched_at for s in cached_stats if s.fetched_at),
                    default=None,
                )
                update_cache(cached_repos, cached_stats, timestamp=newest_fetched_at)
                update_repo_metrics(cached_stats)
                repos = cached_repos
                logger.info("Loaded %d repositories from cache (fallback)", len(cached_repos))
        except Exception:
            logger.exception("Failed to load cached data from database")

    # Workspace manager
    workspace = WorkspaceManager(config)
    try:
        await workspace.setup(repos)
        logger.info("Workspace setup complete for %d repositories", len(repos))
    except Exception:
        logger.exception("Workspace setup failed — checks/actions may not work")

    # Checks & actions
    checks = load_checks(config.data_dir)
    actions = load_actions(config.data_dir)

    # Restore persisted toggle state from DB
    await restore_check_toggles(engine, checks)
    await restore_action_toggles(engine, actions)

    set_checks_state(checks, repos, workspace, engine)
    set_actions_state(actions, repos, workspace, engine)

    # Refresh callback for POST /api/refresh
    async def _do_refresh() -> list[TrackedRepository]:
        with DATA_REFRESH_DURATION.time():
            refreshed_repos, refreshed_stats = await refresh_all_stats(config, client)
        update_cache(refreshed_repos, refreshed_stats)
        update_repo_metrics(refreshed_stats)
        await prune_stale_data(engine, refreshed_repos, config.workspace_dir)

        # Sync local clones with their remotes so worktrees have fresh data
        await workspace.sync_all(refreshed_repos)

        set_checks_state(checks, refreshed_repos, workspace, engine)
        set_actions_state(actions, refreshed_repos, workspace, engine)

        # Run default-schedule checks (those without a cron schedule)
        default_checks = [c for c in checks if c.enabled and not c.schedule]
        for check in default_checks:
            try:
                await run_check_for_all_targets(
                    check, refreshed_repos, workspace, engine, triggered_by="refresh"
                )
            except Exception:
                logger.exception("Default-schedule check '%s' failed", check.slug)

        return refreshed_repos

    set_refresh_callback(_do_refresh)

    # Scheduler
    scheduler = AsyncIOScheduler()

    # Periodic data refresh
    refresh_trigger = CronTrigger.from_crontab(config.refresh_schedule)
    scheduler.add_job(
        _do_refresh,
        trigger=refresh_trigger,
        id="data-refresh",
        replace_existing=True,
    )

    register_checks(scheduler, checks, repos, workspace, engine)
    register_actions(scheduler, actions, repos, workspace, engine)

    scheduler.start()
    logger.info("Scheduler started (refresh schedule: %s)", config.refresh_schedule)

    # Launch background refresh if needed (server is already accepting requests)
    if need_background_refresh:

        async def _initial_refresh() -> None:
            try:
                logger.info("Background initial refresh starting")
                refreshed_repos = await _do_refresh()
                try:
                    await workspace.setup(refreshed_repos)
                except Exception:
                    logger.exception("Workspace re-setup failed after initial refresh")
                logger.info(
                    "Background initial refresh complete — %d repos",
                    len(refreshed_repos),
                )
            except Exception:
                logger.exception("Background initial refresh failed")

        asyncio.create_task(_initial_refresh())

    yield

    # Shutdown
    if scheduler:
        scheduler.shutdown(wait=False)
    if client:
        await client.close()
    logger.info("Grimoire shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Grimoire",
        description="Self-hostable GitHub repository monitoring dashboard",
        version=VERSION,
        lifespan=lifespan,
    )

    # API routers
    app.include_router(repos_router, prefix="/api")
    app.include_router(refresh_router, prefix="/api")
    app.include_router(checks_router, prefix="/api")
    app.include_router(actions_router, prefix="/api")

    # Observability
    app.include_router(metrics_router)

    # Web pages
    app.include_router(web_router)

    # Static files
    static_dir = Path(__file__).parent / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health", tags=["health"])
    async def health() -> JSONResponse:
        """Health check endpoint."""
        status = "ok"
        details: dict[str, object] = {}

        if _last_refresh:
            age = (datetime.now(tz=timezone.utc) - _last_refresh).total_seconds()
            details["cache_age_seconds"] = int(age)

        if _cache:
            details["tracked_repos"] = len(_cache)

        return JSONResponse({"status": status, "version": VERSION, **details})

    return app
