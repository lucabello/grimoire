# Write a check

This guide shows you how to create a custom check that validates a standard across your repositories.

## Create the check file

Checks are YAML files stored in `data/checks/`. The filename becomes the check's slug (identifier).

Create `data/checks/has-readme.yaml`:

```yaml
name: Has README
description: Ensures every tracked repo has a README.md

targets:
  regex: ".*"   # all repos

script: |
  test -f README.md
```

That's it — Grimoire picks up new check files automatically on the next refresh cycle.

## Understand the structure

Every check definition has these fields:

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name on the dashboard |
| `description` | Yes | What the check validates |
| `targets` | Yes | Which repos to run against (see [targeting](#targeting)) |
| `script` | Yes | Bash script to execute |
| `schedule` | No | Cron expression; defaults to `refresh_schedule` |
| `enabled` | No | `true` (default) or `false` |
| `severity` | No | `"error"` (default) or `"warning"` — controls how failures appear on the dashboard |

!!! tip "Need a tool in your script?"

    In Docker, declare extra tools (`jq`, `shellcheck`, ...) in a `mise.toml` next to `config.yaml`. See [Install tools for your checks](install-tools.md).

## Exit codes

A check script communicates its result via exit code:

| Exit code | Result |
|-----------|--------|
| `0` | ✅ Pass |
| Any non-zero | ❌ Fail (reported as `error` or `warning` based on `severity`) |

## Script environment

Scripts execute inside the cloned repository directory for the target branch. Available environment variables:

| Variable | Description |
|----------|-------------|
| `REPO_OWNER` | Repository owner |
| `REPO_NAME` | Repository name |
| `REPO_FULL_NAME` | `owner/name` |
| `BRANCH` | Branch being checked |
| `DEFAULT_BRANCH` | Default branch of the repository |

Scripts have a **5-minute timeout** and output is capped at 64 KB.

## Targeting

The `targets` field determines which repos a check runs against. You must specify exactly one strategy:

### All repos (regex match-all)

```yaml
targets:
  regex: ".*"
```

### Specific repos (explicit list)

```yaml
targets:
  list:
    - "owner/repo-a"
    - "owner/repo-b"
```

### Pattern match (regex)

```yaml
targets:
  regex: "myorg/.*-service"
```

### Dynamic (script) — also branch-level

`list` and `regex` include every observed branch of a matched repo. Use `script` targeting when you need to scope down: the script is evaluated once per (repo, branch), and the `(repo, branch)` is included only if it exits `0`.

```yaml
# Only branches that have a Python project
targets:
  script: |
    test -f pyproject.toml
```

Common patterns:

```yaml
# Default branch only
targets:
  script: '[ "$BRANCH" = "$DEFAULT_BRANCH" ]'
```

```yaml
# release/* branches only
targets:
  script: |
    case "$BRANCH" in release/*) exit 0 ;; *) exit 1 ;; esac
```

The target script receives the same environment as the check script (`REPO_OWNER`, `BRANCH`, `DEFAULT_BRANCH`, `GH_TOKEN`, ...).

## Set severity

By default, check failures are reported as errors. For advisory checks, set severity to `warning`:

```yaml
severity: warning
```

Warnings appear with a yellow indicator on the dashboard instead of red, and carry a lower weight in the backlog.

## Set a custom schedule

By default, checks run on the global `refresh_schedule`. Override with a cron expression:

```yaml
schedule: "0 * * * *"   # hourly
```

## Trigger manually

```bash
# Run against all targeted repos
curl -X POST http://localhost:8000/api/checks/has-readme/run

# Run against a specific repo
curl -X POST "http://localhost:8000/api/checks/has-readme/run?repo=owner/repo"
```

## Disable a check

Toggle from the API:

```bash
curl -X POST http://localhost:8000/api/checks/has-readme/toggle
```

Or set `enabled: false` in the YAML file.

## Example: Lint check with severity

```yaml
# data/checks/no-fixme-comments.yaml
name: No FIXME comments
description: Warns when FIXME comments are found in source code
severity: warning

targets:
  regex: ".*"

schedule: "0 6 * * *"   # daily at 6am

script: |
  ! grep -r "FIXME" --include="*.py" .
```
