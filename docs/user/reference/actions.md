# Actions reference

Technical reference for action definition files.

## File location

Action definitions are YAML files in the `data/actions/` directory. The filename (without `.yaml`) becomes the action's **slug**.

```
data/actions/
├── update-copyright.yaml   → slug: "update-copyright"
└── sync-templates.yaml     → slug: "sync-templates"
```

## Definition schema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | — | Display name shown on the dashboard |
| `description` | string | Yes | — | What the action does |
| `targets` | [TargetSpec](#target-spec) | No | `null` | Which repos to run against; `null` = global action |
| `script` | string | Yes | — | Bash script to execute |
| `schedule` | string (cron) | No | `null` | Cron schedule; `null` = manual-only |
| `enabled` | boolean | No | `true` | Whether the action runs on schedule |

## Target spec {: #target-spec }

Works identically to [checks targeting](checks.md#target-spec). Exactly one of `list`, `regex`, or `script` must be set — or the field can be omitted entirely for a global action.

- `list` / `regex` match by repo full name and run the action against **every observed branch** of a matched repo.
- `script` is evaluated **per (repo, branch)** in that branch's workdir; the branch is included only when the script exits `0`. The target script sees the same env vars as the action script (`BRANCH`, `DEFAULT_BRANCH`, ...), so it doubles as a branch filter:

    ```yaml
    # Default branch only (e.g. actions that open PRs)
    targets:
      script: '[ "$BRANCH" = "$DEFAULT_BRANCH" ]'
    ```

**If `targets` is omitted entirely**, the action is **global** — it runs once in the workspace root without iterating over repositories or branches. Global actions do not receive `REPO_*` / `BRANCH` / `DEFAULT_BRANCH` env vars.

## Exit codes

| Exit code | Result |
|-----------|--------|
| `0` | Success |
| Non-zero | Failure |

## Script execution environment

| Property | Value |
|----------|-------|
| Working directory | Cloned repo at target branch (or workspace root for global actions) |
| Timeout | 600 seconds (10 minutes) |
| Output cap | 64 KB (stdout + stderr) |
| Shell | `/bin/sh` (override with a shebang, e.g. `#!/usr/bin/env bash`) |

### Environment variables

| Variable | Description |
|----------|-------------|
| `REPO_OWNER` | Repository owner |
| `REPO_NAME` | Repository name |
| `REPO_FULL_NAME` | `owner/name` |
| `BRANCH` | Current branch |
| `DEFAULT_BRANCH` | Default branch of the repository |
| `GH_TOKEN` / `GITHUB_TOKEN` | GitHub token (for `gh` CLI) |

The `gh` CLI is pre-installed in the Docker image. For other tools, see [Install tools for your checks](../how-to/install-tools.md).

## Concurrency

Only one instance of a given action can run at a time. A second trigger returns HTTP `409 Conflict`.

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/actions` | List all action definitions |
| `GET` | `/api/actions/{slug}/runs` | Run history (reverse chronological) |
| `GET` | `/api/actions/{slug}/runs/{id}` | Specific run details + logs |
| `POST` | `/api/actions/{slug}/run` | Trigger action; optional `?repo=owner/name`; 409 if already running |
