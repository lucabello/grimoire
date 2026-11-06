# Configuration reference

Complete reference for Grimoire's `config.yaml` file.

All string values support environment variable references with the syntax `"${ENV_VAR}"`. Only exact-match strings are resolved — no partial interpolation.

## Config file resolution

Grimoire looks for its configuration in this order:

1. `--config-file` passed to the `grimoire` command
2. `GRIMOIRE_CONFIG` environment variable
3. `./config.yaml` in the working directory
4. `~/.config/grimoire/config.yaml` (XDG config path)

To run several instances side by side, give each its own config file and port:

```bash
uv run grimoire --config-file config.mlops.yaml --port 8001
```

`grimoire --help` lists all options (`--config-file`, `--port`, `--host`).

In Docker, a `mise.toml` in the same directory as the config file declares extra tools for your checks. See [Install tools for your checks](../how-to/install-tools.md).

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

## `dashboard` {: #dashboard }

**Optional.** Dashboard presentation options.

### `workflow_groups`

Split CI workflows into separate dashboard columns, for example to make release or nightly pipelines stand out. Each group has a `name` (the column header) and a list of `match` glob patterns tested against the workflow name. Matching is case-sensitive (`*release*` does not match `Release`). The first group that matches wins.

```yaml
dashboard:
  workflow_groups:
    - name: "Release"
      match: ["*Release*", "*Publish*"]
    - name: "Nightly"
      match: ["nightly*"]
  show_other: true
```

| Field | Default | Description |
|-------|---------|-------------|
| `workflow_groups` | `[]` | Ordered list of groups. Group names must be non-empty and unique, and `match` must not be empty. With `show_other: true`, no group may be named `Other`. |
| `show_other` | `true` | Show a trailing **Other** column for workflows matching no group. Set to `false` to hide them. |

Without `workflow_groups`, all workflows share a single **Workflows** column. The grouping also applies to the dashboard list view and the repository page.

Hidden workflows (`show_other: false`) are only hidden from display: they still count toward a repository's health status. The matrix has one column per group, so keep the number of groups small.

---

## `maintenance` {: #maintenance }

**Optional.** Groups of pull requests that you want to look at together across all tracked repositories, for example Renovate PRs or the PRs of one person. Grimoire does not fetch or store these PRs: for each group it builds a **GitHub search query** over your tracked repositories (including the ones resolved from teams) and a link that opens it on GitHub. The groups are listed on the **Maintenance** page and in the [REST API](api.md#maintenance).

```yaml
maintenance:
  groups:
    - name: "renovate"
      authors: ["app/renovate"]
      labels: ["dependencies"]
      status: failure
      repos: ["org-name/*"]
    - name: "alice"
      authors: ["alice"]
      draft: false
```

| Field | Description |
|-------|-------------|
| `name` | Required, non-blank, unique among the groups. |
| `authors` | PR authors. Bots are written `app/<name>` (for example `app/renovate`). |
| `labels` | PR labels. |
| `title_words` | Words or phrases searched in the PR title. They are plain words, not a regular expression: GitHub search has no regex. |
| `draft` | `true` for draft PRs only, `false` for PRs ready for review. |
| `base` | Target branch of the PR. |
| `status` | Commit status: `success`, `failure` or `pending`. GitHub's `status:pending` also matches PRs that have no statuses at all (on `canonical/operator`, 23 of 33 open PRs matched `pending`). |
| `repos` | Optional. Case-sensitive glob patterns on the tracked repository full names (`org-name/*`). Default: all tracked repositories. |

At least one of `authors`, `labels`, `title_words`, `draft`, `base` or `status` is required, and list entries must not be blank or contain a double quote. Several values in one list are alternatives (any author, any label, any word); different fields must all match. The query always starts with `is:pr is:open`.

The query lists the tracked repositories one by one with `repo:` qualifiers, so it never includes a repository Grimoire does not track. A group whose `repos` patterns match no tracked repository has no link. GitHub refuses very long links (about 8 KB), so with a very large number of repositories a group is split into several links ("Part 1 of 2", …); open every part.

!!! warning "Check one multi-value link"
    The queries use GitHub's advanced search syntax (`( … OR … )`). GitHub's API evaluates it when asked (`advanced_search=true`), but what `https://github.com/pulls?q=` does with a parenthesised query is **unverified**: GitHub's documentation names the repository issues page and the issues dashboard, not the pull requests page, and the page needs a login so it could not be tested. Before relying on a group with several authors, labels, title words or repositories, click one of its links and confirm the result matches what you expect. Groups with a single value per field and one repository use plain qualifiers.

    Values that are search keywords (`OR`, `AND`, `NOT`) or that start with `-` are quoted, so they are searched as literal words.

    To run a query yourself, see the [API reference](api.md#maintenance).

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

maintenance:
  groups:
    - name: "renovate"
      authors: ["app/renovate"]
      labels: ["dependencies"]
      repos: ["org-name/*"]

refresh_schedule: "*/5 * * * *"

data_dir: "./data"
workspace_dir: "./workspace"
database_path: "./grimoire.db"
log_file: "./grimoire.log"
```
