# Architecture

This page explains how Grimoire works internally, to help you understand its behaviour and make informed configuration decisions.

## Overview

Grimoire is a single-process Python application built on FastAPI. It combines:

- A **scheduler** (APScheduler) that periodically fetches data from the GitHub API
- A **check/action engine** that runs user-defined scripts against cloned repos
- A **web dashboard** (Jinja2 + HTMX) and REST API for visibility and control
- A **SQLite database** for persistence (zero external dependencies)

## Data flow

```
GitHub API
    │
    ▼
┌──────────────────┐
│  GitHub Service   │──► SQLite (cache + results)
│  (httpx client)   │
└──────────────────┘
    │
    ▼
┌──────────────────┐     ┌──────────────────┐
│  Checks Engine    │────►│  Workspace Mgr   │
│  (validates)      │     │  (git clone/pull)│
└──────────────────┘     └──────────────────┘
    │
    ▼
┌──────────────────┐
│  Actions Engine   │────► git push, gh CLI
│  (remediates)     │
└──────────────────┘
    │
    ▼
┌──────────────────┐
│  Web Dashboard    │◄── HTMX polling
│  (FastAPI+Jinja2) │
└──────────────────┘
```

## Caching and efficiency

Grimoire is designed to be friendly to the GitHub API rate limit:

- **Conditional requests**: Uses ETags to avoid re-downloading unchanged data. After the initial fetch, most refreshes cost zero API calls.
- **`per_page=100`**: Maximises data per request.
- **Batched GraphQL for PRs**: The CI status of the pull requests that match a PR group is read with one GraphQL request per 20 PRs, from a rate-limit bucket separate from REST. The PRs themselves come from the pull request lists that are fetched anyway.
- **Bounded concurrency**: A semaphore limits parallel API calls to 10.
- **SQLite persistence**: On restart, cached data is loaded immediately. The dashboard is usable before the first refresh completes.

## Scheduling

Grimoire uses APScheduler (v3) with cron triggers:

- The global `refresh_schedule` controls how often repo data is fetched and default checks run.
- Checks and actions can override this with their own `schedule` field.
- Everything runs in-process — no external queue or worker needed.

## Workspace management

Checks and actions run against cloned copies of your repositories:

- Repos are cloned to `workspace_dir` on first use.
- Before each check/action run, the workspace is reset to the target branch head (`git fetch` + `git reset --hard`).
- Each check runs in an isolated temporary directory to avoid state leakage.

## Database

Grimoire uses SQLite via aiosqlite + SQLModel. The database stores:

- Cached GitHub data (repos, issues, PRs, workflow statuses)
- Check and action results with full output logs
- Toggle states (enabled/disabled per check/action)

The schema is migrated automatically at startup: a new database is created, and an existing one — including one from a release that predates migrations — is upgraded in place. Upgrading Grimoire never requires deleting `grimoire.db`; toggles, run history and the GitHub cache are kept.

## Security model

!!! warning "Trust boundary"

    Grimoire executes user-provided bash scripts with the same privileges as the
    Grimoire process. It is designed for **self-hosted, trusted environments** only.

- No authentication is built in — use a reverse proxy for access control.
- The GitHub token has access to all configured repositories.
- Actions can push commits, create PRs, and call `gh` CLI with the token.
