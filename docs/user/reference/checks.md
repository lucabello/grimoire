# Checks reference

Technical reference for check definition files.

## File location

Check definitions are YAML files in the `data/checks/` directory. The filename (without `.yaml`) becomes the check's **slug** — its unique identifier.

```
data/checks/
├── has-readme.yaml       → slug: "has-readme"
├── uv-lock-fresh.yaml    → slug: "uv-lock-fresh"
└── no-fixme.yaml         → slug: "no-fixme"
```

Slug uniqueness is enforced — duplicate filenames cause a startup error.

## Definition schema

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `name` | string | Yes | — | Display name shown on the dashboard |
| `description` | string | Yes | — | What the check validates |
| `targets` | [TargetSpec](#target-spec) | Yes | — | Which repos to run against |
| `script` | string | Yes | — | Bash script to execute |
| `schedule` | string (cron) | No | `refresh_schedule` | Custom cron schedule |
| `enabled` | boolean | No | `true` | Whether the check runs on schedule |
| `severity` | `"error"` \| `"warning"` | No | `"error"` | How failures appear on the dashboard and in the backlog |
| `spaces` | list of strings | No | `[]` | Space ids under which the check is listed; empty = listed under every space |

`spaces` only controls where the check is **listed** when you filter by space. It does not change which repositories the check runs against: `targets` alone decides that. Every id must be defined in the configuration's `spaces`, otherwise Grimoire refuses to start and names the check and the id.

## Target spec {: #target-spec }

The `targets` field specifies which repositories a check runs against. Exactly **one** of the following strategies must be set:

### `list` — explicit repos

Matches whole repos; all observed branches are checked:

```yaml
targets:
  list:
    - "owner/repo-a"
    - "owner/repo-b"
```

### `regex` — pattern match

Matched against the full repo name (`owner/name`); all observed branches are checked:

```yaml
targets:
  regex: "myorg/.*-service"
```

Use `".*"` to match all tracked repos.

### `script` — dynamic (per-branch) filtering

Runs the script once for **each observed branch** of every candidate repo, in that branch's workdir. The `(repo, branch)` pair is included if the script exits `0`. This is how you scope a check to specific branches:

```yaml
targets:
  # File-existence gate — runs on every branch that has pyproject.toml
  script: |
    test -f pyproject.toml
```

```yaml
targets:
  # Default branch only
  script: '[ "$BRANCH" = "$DEFAULT_BRANCH" ]'
```

```yaml
targets:
  # Release branches only
  script: |
    case "$BRANCH" in release/*) exit 0 ;; *) exit 1 ;; esac
```

Script targeting has a 30-second timeout per branch. The target script sees the same environment variables as the check script itself (see below).

## Exit codes

| Exit code | Result |
|-----------|--------|
| `0` | Pass |
| Non-zero | Fail |

The `severity` field controls whether a failure is reported as an **error** (red, high backlog weight) or a **warning** (yellow, low backlog weight).

## Script execution environment

| Property | Value |
|----------|-------|
| Working directory | Cloned repo at the target branch |
| Timeout | 300 seconds (5 minutes) |
| Output cap | 64 KB (stdout + stderr) |
| Shell | `/bin/sh` (override with a shebang, e.g. `#!/usr/bin/env bash`) |

### Environment variables

| Variable | Description |
|----------|-------------|
| `REPO_OWNER` | Repository owner |
| `REPO_NAME` | Repository name |
| `REPO_FULL_NAME` | `owner/name` |
| `BRANCH` | Branch being checked |
| `DEFAULT_BRANCH` | Default branch of the repository |
| `GH_TOKEN` / `GITHUB_TOKEN` | GitHub API token |

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/checks` | List all check definitions and enabled state |
| `GET` | `/api/checks/{slug}/results` | Latest results for a check |
| `GET` | `/api/checks/{slug}/runs` | Run history (reverse chronological) |
| `POST` | `/api/checks/{slug}/run` | Trigger check; optional `?repo=owner/name` |
| `POST` | `/api/checks/{slug}/toggle` | Toggle enabled state |
