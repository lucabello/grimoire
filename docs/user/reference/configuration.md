# Configuration reference

Complete reference for Grimoire's `config.yaml` file.

All string values support environment variable references with the syntax `"${ENV_VAR}"`. Only exact-match strings are resolved — no partial interpolation.

## Config file resolution

Grimoire looks for its configuration in this order:

1. Explicit path passed at startup
2. `GRIMOIRE_CONFIG` environment variable
3. `./config.yaml` in the working directory
4. `~/.config/grimoire/config.yaml` (XDG config path)

---

## `github` {: #github }

**Required.** GitHub API authentication.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `token` | string | Yes | GitHub personal access token. |

```yaml
github:
  token: "${GITHUB_TOKEN}"
```

---

## `git` — Git identity {: #git }

**Optional.** Only needed if you use actions that create commits or push changes.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `user.name` | string | Yes (if `git` set) | Git committer name |
| `user.email` | string | Yes (if `git` set) | Git committer email |
| `signing.key_path` | path | No | Path to signing key (SSH or GPG) |
| `signing.format` | `"ssh"` \| `"gpg"` | No | Signing key format |
| `ssh_known_hosts` | path | No | Path to SSH `known_hosts` file |

```yaml
git:
  user:
    name: "Grimoire Bot"
    email: "grimoire@example.com"
  signing:
    key_path: "/keys/id_ed25519"
    format: "ssh"
  ssh_known_hosts: "/keys/known_hosts"
```

---

## `repositories` {: #repositories }

**Required.** A list of repository sources to monitor. Each entry is one of:

### Static repo

Track a single repository by full name.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `repo` | string | Yes | Full name (`owner/repo`) |
| `branches` | list of strings | No | Branches to track; defaults to default branch only |
| `workflows.include` | list of strings | No | Glob patterns — only track matching workflows |
| `workflows.exclude` | list of strings | No | Glob patterns — exclude matching workflows (applied after include) |

```yaml
repositories:
  - repo: "owner/repo-name"
    branches: ["main", "develop"]
    workflows:
      include: ["CI", "Tests *"]
      exclude: ["Publish *"]
```

### Team source

Track all repositories belonging to a GitHub team. Archived repos are always excluded.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `team` | string | Yes | Team slug (`org/team-name`) |
| `exclude` | list of strings | No | Repos to exclude (full names) |
| `workflows.include` | list of strings | No | Glob patterns — only track matching workflows |
| `workflows.exclude` | list of strings | No | Glob patterns — exclude matching workflows |

```yaml
repositories:
  - team: "org-name/team-slug"
    exclude:
      - "org-name/excluded-repo"
    workflows:
      exclude: ["Release *"]
```

---

## `staleness` {: #staleness }

**Optional.** Thresholds for marking issues and PRs as stale.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `pull_requests_days` | int | `30` | PRs with no activity for this many days are stale |
| `issues_days` | int | `365` | Issues with no activity for this many days are stale |
| `problematic_stale_issues_pct` | int | `20` | Highlight stale issue counts when `stale/open >= N%` |
| `problematic_stale_prs_pct` | int | `20` | Highlight stale PR counts when `stale/open >= N%` |

```yaml
staleness:
  pull_requests_days: 30
  issues_days: 365
  problematic_stale_issues_pct: 20
  problematic_stale_prs_pct: 20
```

### Health status display

The thresholds above control *when* issues/PRs count as stale and *how* stale counts are highlighted — they don't control whether staleness affects a repo's overall health status (the left-border accent / status icon on the dashboard).

That's a separate, per-browser display preference: click the ⚙ icon next to the dashboard's view switcher to toggle whether **check results** and/or **stale issues/PRs** are factored into the computed health status. Workflow failures always count. This preference is stored in your browser's `localStorage`, not in `config.yaml` — it doesn't affect other viewers of the same Grimoire instance.

---

## `backlog` {: #backlog }

**Optional.** Controls how the backlog page ranks and scores problems.

### `category_weights`

Base importance score for each problem type.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `failing_workflow` | float | `100` | Failing CI workflow |
| `failing_check_error` | float | `80` | Check failure (error severity) |
| `failing_check_warning` | float | `30` | Check failure (warning severity) |
| `stale_pr` | float | `50` | Stale pull request |
| `stale_issue` | float | `20` | Stale issue |

### `workflow_weights`

Per-workflow multiplier using glob patterns on workflow name.

```yaml
backlog:
  workflow_weights:
    "Release *": 2.0
    "Lint": 0.5
```

### `repository_weights`

Per-repository multiplier. Rules are evaluated top-to-bottom; last match wins. Each rule must have exactly one of `regex` or `repos`.

| Key | Type | Description |
|-----|------|-------------|
| `regex` | string | fnmatch glob pattern matched against repo full name |
| `repos` | list of strings | Explicit list of repo full names |
| `weight` | float | Multiplier applied to backlog scores (default: `1.0`) |

```yaml
backlog:
  category_weights:
    failing_workflow: 100
    failing_check_error: 80
    failing_check_warning: 30
    stale_pr: 50
    stale_issue: 20
  workflow_weights:
    "Release *": 2.0
    "Lint": 0.5
  repository_weights:
    - regex: "*"
      weight: 1.0
    - regex: "*-operator"
      weight: 3.0
    - repos:
        - "org/critical-repo"
      weight: 5.0
```

---

## `refresh_schedule` {: #refresh-schedule }

**Optional.** Cron expression controlling how often Grimoire fetches fresh data from GitHub and runs checks with no custom schedule.

| Type | Default |
|------|---------|
| string (cron) | `"*/5 * * * *"` |

```yaml
refresh_schedule: "*/5 * * * *"
```

---

## Paths {: #paths }

**Optional.** Override default file locations. Defaults use XDG paths (`~/.local/share/grimoire/`).

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `data_dir` | path | `~/.local/share/grimoire/data` | Directory containing `checks/` and `actions/` YAML files |
| `workspace_dir` | path | `~/.local/share/grimoire/workspace` | Where repositories are cloned |
| `database_path` | path | `~/.local/share/grimoire/grimoire.db` | SQLite database file |
| `log_file` | path | `~/.local/share/grimoire/grimoire.log` | Application log file |

!!! tip "XDG compliance"
    If `$XDG_DATA_HOME` is set, Grimoire uses `$XDG_DATA_HOME/grimoire/` instead of `~/.local/share/grimoire/`.

```yaml
data_dir: "./data"
workspace_dir: "./workspace"
database_path: "./grimoire.db"
log_file: "./grimoire.log"
```

---

## Full example

```yaml
github:
  token: "${GITHUB_TOKEN}"

git:
  user:
    name: "Grimoire Bot"
    email: "grimoire@example.com"
  signing:
    key_path: "/keys/id_ed25519"
    format: "ssh"
  ssh_known_hosts: "/keys/known_hosts"

repositories:
  - repo: "owner/repo-name"
    branches: ["main", "develop"]
  - repo: "owner/another-repo"
  - team: "org-name/team-slug"
    exclude:
      - "org-name/excluded-repo"
    workflows:
      exclude: ["Release *"]

staleness:
  pull_requests_days: 30
  issues_days: 365
  problematic_stale_issues_pct: 20
  problematic_stale_prs_pct: 20

backlog:
  category_weights:
    failing_workflow: 100
    failing_check_error: 80
    failing_check_warning: 30
    stale_pr: 50
    stale_issue: 20
  workflow_weights:
    "Release *": 2.0
  repository_weights:
    - regex: "*"
      weight: 1.0
    - repos:
        - "org/critical-repo"
      weight: 5.0

refresh_schedule: "*/5 * * * *"

data_dir: "./data"
workspace_dir: "./workspace"
database_path: "./grimoire.db"
log_file: "./grimoire.log"
```
