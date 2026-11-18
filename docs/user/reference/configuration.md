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

**Required** unless [`spaces`](#spaces) or [`space_files`](#space-files) provide at least one repository source. A list of repository sources to monitor. Each entry is one of:

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

Repositories listed here and in no [space](#spaces) are shown as **Unassigned** once spaces are configured.

---

## `spaces` {: #spaces }

**Optional.** Named slices of the tracked repositories, so that several teams can share one Grimoire. When spaces are configured, a selector in the navigation bar filters every page by space. See [Host Grimoire for several teams](../how-to/host-for-several-teams.md) for a walkthrough and [Spaces](../explanation/architecture.md#spaces) for how they behave.

Each entry has these keys and no others; an unknown key is a startup error.

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `id` | string | Yes | Identifier used in `?spaces=`, in the `spaces` field of [checks](checks.md) and [actions](actions.md), and in the metrics. Must match `^[a-z0-9][a-z0-9-]*$`: lower-case letters, digits and `-`, not starting with `-` or `_`. Unique across inline spaces and space files. |
| `name` | string | Yes | Display name. |
| `color` | string | No | `#rrggbb`, used for the dots in the selector and the badges. |
| `description` | string | No | Default `""`. Shown as a tooltip in the selector. |
| `repositories` | list | Yes, non-empty | Repository sources, with the same keys as the top-level [`repositories`](#repositories): static repos and team sources, including `branches`, `exclude` and `workflows`. |

```yaml
spaces:
  - id: platform
    name: "Platform"
    color: "#3366ff"
    description: "Platform team"
    repositories:
      - repo: "org-name/api"
      - team: "org-name/platform"
```

### Repositories in several spaces

A repository may be listed in several spaces and in the top-level `repositories`. Grimoire fetches and clones it once. Its branches are the union of the branches of every source that lists it. A workflow filter (`workflows.include` / `workflows.exclude`) belongs to the source that sets it:

- A workflow is fetched and kept if **any** source allows it.
- A workflow is shown if any source of a **selected** space allows it. With several spaces selected, the filters are combined; with **All** selected, every source counts.

### Unassigned

When spaces are configured, repositories that only appear in the top-level `repositories` belong to no space. The selector then offers an **Unassigned** entry (id `_unassigned` in `?spaces=`), only while such repositories exist. `_unassigned` cannot be used as a space `id` or in the `spaces` field of a check or action.

---

## `space_files` {: #space-files }

**Optional.** A list of glob patterns for files that each define one space, so that each team can own its own file. Patterns are relative to the directory of the main config file and may use `**`. Files matched by one pattern are loaded in sorted order; a file matched by several patterns is loaded once; a pattern that matches nothing is not an error.

```yaml
space_files: ["spaces.d/*.yaml"]
```

```yaml
# spaces.d/payments.yaml — the same keys as one entry of `spaces`
id: payments
name: "Payments"
color: "#e8590c"
repositories:
  - repo: "org-name/payments-api"
  - repo: "org-name/shared-lib"
    workflows:
      include: ["CI"]
```

Rules for space files and inline spaces:

- A file must be a YAML mapping describing exactly one space, with only the space keys above. `${ENV_VAR}` references are resolved as in the main file.
- The spaces from files are appended after the inline ones. There are no overrides: a duplicate `id` (inline and file, or two files) is a startup error that names both places.
- Other settings (`staleness`, `backlog`, `dashboard`, `prs`, `refresh_schedule`) are global; a space cannot override them.

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

## `prs` {: #prs }

**Optional.** Named groups of the open pull requests of your tracked repositories (including the ones resolved from teams), shown on the **PRs** page with their CI status, for example the PRs opened by Renovate, Dependabot or a bot of your own. The groups are also served by the [REST API](api.md#prs).

```yaml
prs:
  groups:
    - name: "Maintenance"
      authors: ["app/renovate", "app/dependabot", "my-team-bot"]
    - name: "alice"
      authors: ["alice"]
      draft: false
      repos: ["org-name/*"]
```

| Field | Description |
|-------|-------------|
| `name` | Required, non-blank, unique among the groups. |
| `authors` | PR authors. Case is ignored, and so are a leading `app/` and a trailing `[bot]`: `app/renovate` matches the PRs of `renovate[bot]`. |
| `labels` | PR labels (case-insensitive, exact name). |
| `title_words` | Words or phrases that must appear in the PR title (case-insensitive substring, not a regular expression). |
| `draft` | `true` for draft PRs only, `false` for PRs ready for review. |
| `base` | Target branch of the PR. |
| `repos` | Optional. Case-sensitive glob patterns on the tracked repository full names (`org-name/*`). Default: all tracked repositories. |

At least one of `authors`, `labels`, `title_words`, `draft` or `base` is required, and list entries must not be blank. Several values in one list are alternatives (any author, any label, any word); different fields must all match. A PR that matches several groups is listed in each of them.

Grimoire reads the PRs from the pull request lists it already fetches for the dashboard, so they cost no additional API request. The CI, review and merge state of the PRs that match a group is read with one [GraphQL](https://docs.github.com/en/graphql) request per 20 PRs, using the same token. That uses GitHub's separate GraphQL rate limit (one point per request) and nothing when no group is configured. If GitHub cannot answer, the PRs are still listed (with their last known state, if any) and the page shows a warning.

!!! note "Pending CI"
    The CI badge shows the combined state GitHub reports for the PR's latest commit. A Renovate PR that waits for a `stability-days` check is pending until that delay passes.

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

prs:
  groups:
    - name: "Maintenance"
      authors: ["app/renovate", "app/dependabot"]
      repos: ["org-name/*"]

spaces:
  - id: platform
    name: "Platform"
    color: "#3366ff"
    repositories:
      - repo: "owner/repo-name"
      - team: "org-name/platform"

space_files: ["spaces.d/*.yaml"]

refresh_schedule: "*/5 * * * *"

data_dir: "./data"
workspace_dir: "./workspace"
database_path: "./grimoire.db"
log_file: "./grimoire.log"
```
