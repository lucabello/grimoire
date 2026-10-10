# Write an action

This guide shows you how to create an automated action that modifies repositories — creating commits, PRs, or running maintenance tasks.

## Create the action file

Actions are YAML files stored in `data/actions/`. The filename becomes the action's slug.

Create `data/actions/update-copyright.yaml`:

```yaml
name: Update Copyright Year
description: Updates the copyright year in LICENSE files

targets:
  regex: "myorg/.*"

script: |
  sed -i "s/Copyright (c) [0-9]*/Copyright (c) $(date +%Y)/" LICENSE
  git add LICENSE
  git commit -m "chore: update copyright year"
  git push
```

## Understand the structure

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name on the dashboard |
| `description` | Yes | What the action does |
| `targets` | No | Which repos to run against; `null` = global action (runs once, no per-repo iteration) |
| `script` | Yes | Bash script to execute |
| `schedule` | No | Cron expression; omit for manual-only |
| `enabled` | No | `true` (default) or `false` |

## Targeting

Targeting works the same as for [checks](write-a-check.md#targeting) — use `list`, `regex`, or `script`. Just like checks, `script` targeting is evaluated per (repo, branch) and doubles as a branch filter (e.g. `'[ "$BRANCH" = "$DEFAULT_BRANCH" ]'` to open PRs only against the default branch).

If you omit `targets` entirely, the action is **global** — it runs once without iterating over repos. Useful for cross-repo tasks or infrastructure automation.

## Global actions (no targets)

```yaml
name: Sync templates
description: Copies shared templates to all repos
# No targets field — runs once in the workspace root

script: |
  ./scripts/sync-templates.sh
```

## Script environment

Action scripts execute inside the cloned repository directory. Available environment variables:

| Variable | Description |
|----------|-------------|
| `REPO_OWNER` | Repository owner |
| `REPO_NAME` | Repository name |
| `REPO_FULL_NAME` | `owner/name` |
| `BRANCH` | Current branch |
| `DEFAULT_BRANCH` | Default branch of the repository |
| `GH_TOKEN` / `GITHUB_TOKEN` | GitHub token (for `gh` CLI) |

The `gh` CLI is available inside the Docker container. To add other tools (`jq`, `shellcheck`, ...), see [Install tools for your checks](install-tools.md).

Scripts have a **10-minute timeout** and output is capped at 64 KB.

## Git identity

For actions that commit, configure the `git` section in `config.yaml`:

```yaml
git:
  user:
    name: "Grimoire Bot"
    email: "grimoire@example.com"
  signing:
    key_path: "/keys/id_ed25519"
    format: "ssh"
```

See the [git configuration reference](../reference/configuration.md#git) for details.

## Concurrency

Only one instance of a given action can run at a time. Attempting to trigger an already-running action returns HTTP `409 Conflict`.

## Trigger manually

```bash
# Run against all targeted repos
curl -X POST http://localhost:8000/api/actions/update-copyright/run

# Run against a specific repo
curl -X POST "http://localhost:8000/api/actions/update-copyright/run?repo=owner/repo"
```

## View run history

```bash
# List all runs
curl http://localhost:8000/api/actions/update-copyright/runs

# Get details for a specific run
curl http://localhost:8000/api/actions/update-copyright/runs/1
```

## Schedule an action

```yaml
schedule: "0 0 1 1 *"   # midnight on Jan 1st
```

Omit `schedule` for manual-only actions.

## Differences from checks

| | Checks | Actions |
|---|--------|---------|
| **Purpose** | Validate / detect | Fix / automate |
| **Side effects** | Read-only | Can modify repos |
| **Exit codes** | 0 = pass, non-zero = fail | 0 = success, non-zero = failure |
| **Severity** | `error` or `warning` | N/A |
| **Git identity** | Not needed | Required for commits |
| **Timeout** | 5 minutes | 10 minutes |
| **Targets** | Required | Optional (null = global) |
