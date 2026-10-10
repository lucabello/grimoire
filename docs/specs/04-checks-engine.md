# Module 4: Checks Engine

Load user-defined checks from YAML files, resolve their targets, execute them against repos, schedule them, and expose REST APIs for management.

**Dependencies:** Module 1 (config, models, database), Module 3 (workspace).

## 4.1 — Check Definition Format

Checks live as YAML files in `{data_dir}/checks/`. Each file defines one check.

```yaml
# data/checks/uv-lock-fresh.yaml
# The filename (without .yaml) becomes the URL slug: "uv-lock-fresh"
name: "UV Lock Fresh"
description: "Ensures the uv.lock file is up to date with pyproject.toml"
targets:
  # Exactly one of: list, regex, script
  regex: "lucabello/.*"
  # list: ["lucabello/repo1", "lucabello/repo2"]
  # script: 'test -f pyproject.toml'                       # per-branch include filter
  # script: '[ "$BRANCH" = "$DEFAULT_BRANCH" ]'            # default branch only
  # script: 'case "$BRANCH" in release/*) ;; *) exit 1;; esac'  # release/* only
script: |
  uv lock --check
schedule: "0 */8 * * *"   # optional cron; omit to use default refresh interval
enabled: true              # optional; default true
severity: error            # optional; "error" (red, default) or "warning" (yellow)
```

**Branch scoping.** `list` and `regex` include every observed branch of a matched repo. `script` is evaluated *per (repo, branch)*, so it doubles as the branch filter: return exit code 0 for branches the check should run against, non-zero for the rest. Branches not configured on the repository in `config.yaml` are never considered.

Scripts run via `/bin/sh` by default. Add a shebang to use a different interpreter:
```yaml
script: |
  #!/usr/bin/env bash
  set -euo pipefail
  # bash-specific features now work (arrays, pipefail, etc.)
```

### Pydantic models

```python
class TargetSpec(BaseModel):
    """Targeting configuration. Exactly one field must be set."""
    list: list[str] | None = None
    regex: str | None = None
    script: str | None = None

    @model_validator(mode="after")
    def exactly_one_target(self) -> Self:
        set_count = sum(1 for v in [self.list, self.regex, self.script] if v is not None)
        if set_count != 1:
            raise ValueError("Exactly one of 'list', 'regex', or 'script' must be set")
        return self

class CheckDefinition(BaseModel):
    name: str
    slug: str               # auto-derived from YAML filename (e.g., "uv-lock-fresh")
    description: str
    targets: TargetSpec
    script: str
    schedule: str | None = None  # cron expression
    enabled: bool = True
    severity: Literal["warning", "error"] = "error"
```

### Severity

The `severity` field controls how a failing check affects the repository's health status on the dashboard:

- **`"error"` (default):** A failure turns the repo accent **red**. Use for critical checks (e.g., tests, lock files).
- **`"warning"`:** A failure is **reported** but does **not** affect the repository's health status. Shown with a yellow ⚠ icon instead of a red ✗. Use for advisory checks (e.g., library freshness).

## 4.2 — Check Loader

**File:** `src/grimoire/checks/loader.py`

```python
def load_checks(data_dir: Path) -> list[CheckDefinition]:
    """
    Load all YAML files from {data_dir}/checks/.
    Parse each into a CheckDefinition model.
    Derive slug from filename (e.g., "uv-lock-fresh.yaml" → "uv-lock-fresh").
    Validate slug uniqueness (no duplicate filenames).
    Raise on validation errors (fail fast at startup).
    """
```

## 4.3 — Target Resolution (shared utility)

**File:** `src/grimoire/targeting.py`

This is a shared utility used by both the checks engine and the actions engine.

```python
ResolvedTarget = tuple[TrackedRepository, list[str]]  # repo + matched branches

async def resolve_targets(
    targets: TargetSpec,
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
) -> list[ResolvedTarget]:
    """
    Resolve which (repo, branch) pairs a check/action applies to.

    - list:   repo matches by full name; all its observed branches are included.
    - regex:  repo matches by full-name pattern; all its observed branches are included.
    - script: the script is executed once per observed branch (in that branch's
              workdir with BRANCH/DEFAULT_BRANCH env vars set); a branch is
              included when the script exits 0. Repos with no matching branch
              are dropped entirely.
    """


def target_env(
    workspace: WorkspaceManager,
    repo: TrackedRepository,
    branch: str,
) -> dict[str, str]:
    """
    Build the subprocess environment for a target/check/action script.

    Includes workspace env vars (GH_TOKEN, git identity, ...) plus per-invocation
    context: REPO_OWNER, REPO_NAME, REPO_FULL_NAME, BRANCH, DEFAULT_BRANCH.
    """
```

**Notes:**
- `list` and `regex` operate on repo full names, so they include *all* observed branches of a matched repo. Use `script` to filter down to specific branches.
- Script targeting has a 30-second timeout per (repo, branch) evaluation.
- `script` receives the same env vars as the check/action itself (`BRANCH`, `DEFAULT_BRANCH`, etc.), so authors can express "default branch only" with `[ "$BRANCH" = "$DEFAULT_BRANCH" ]`, "release branches only" with `case "$BRANCH" in release/*) ;; *) exit 1 ;; esac`, etc.
- Target evaluation is not cached across script invocations; each `(repo, branch)` runs its own subprocess.

## 4.4 — Check Execution Engine

**File:** `src/grimoire/checks/engine.py`

```python
async def run_check(
    check: CheckDefinition,
    repo: TrackedRepository,
    branch: str,
    workspace: WorkspaceManager,
    run_id: int | None = None,
) -> CheckResult:
    """
    1. Call workspace.reset_workdir(full_name, branch) to ensure clean state.
    2. Run check.script as a subprocess in the workdir.
    3. Set env vars (GH_TOKEN, GITHUB_TOKEN, etc.) from workspace.get_env().
    4. Capture stdout+stderr (combined).
    5. Return CheckResult (passed = exit code 0).
    6. Store result in database (CheckResultRecord linked to run_id).
    """

async def run_check_for_all_targets(
    check: CheckDefinition,
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
    triggered_by: str = "manual",
) -> list[CheckResult]:
    """
    1. Create a CheckRunRecord (triggered_by = "manual" | "cron" | "refresh").
    2. Resolve targets → list of (repo, matched_branches).
    3. For each (repo, branch) pair, run the check (linked to run).
    4. Execute concurrently (bounded by semaphore).
    5. Mark CheckRunRecord as completed.
    6. Return all results.
    """
```

**Subprocess execution details:**
- Scripts that begin with a shebang (`#!`) line are written to a temporary file, made executable, and run directly via `create_subprocess_exec` so the OS honors the interpreter (e.g. `#!/usr/bin/env bash`, `#!/usr/bin/env python3`). The temp file is cleaned up after execution.
- Scripts without a shebang are passed to `/bin/sh` via `create_subprocess_shell` (scripts may use pipes, conditionals, etc.).
- Set `cwd` to the workdir path.
- Build the environment with `target_env(workspace, repo, branch)` (workspace env vars + `REPO_OWNER`/`REPO_NAME`/`REPO_FULL_NAME`/`BRANCH`/`DEFAULT_BRANCH`).
- Capture stdout and stderr together (combined stream).
- Apply a timeout (configurable, default 5 minutes per check per repo).
- On timeout, kill the process and record as failure with "Timed out" in output.
- **Output size cap:** Store at most 64KB of output. If exceeded, keep the last 64KB and prepend `"[output truncated — showing last 64KB]\n"`.

### Database records

**`CheckRunRecord`** (table: `check_run`) — groups all results from a single execution:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | Auto-increment |
| `check_slug` | str (indexed) | Which check was run |
| `check_name` | str | Human-readable name |
| `triggered_by` | str | `"manual"` / `"cron"` / `"refresh"` |
| `status` | str | `"running"` / `"completed"` |
| `started_at` | datetime | When the run started |
| `finished_at` | datetime (nullable) | When it completed |

**`CheckResultRecord`** (table: `check_result`) — one row per repo×branch within a run:

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | Auto-increment |
| `run_id` | int FK (nullable, indexed) | Links to `check_run.id` |
| `check_slug` | str (indexed) | Which check |
| `check_name` | str | Human-readable name |
| `repo_full_name` | str (indexed) | Target repository |
| `branch` | str | Branch checked |
| `passed` | bool | Exit code 0 = True |
| `output` | str | stdout + stderr |
| `timestamp` | datetime | When this result was recorded |

## 4.5 — External Tool Dependencies
Check scripts may require tools not bundled in the base Docker image (e.g., `charmcraft`, `shellcheck`, Go binaries, custom CLIs). There are two mechanisms, applied in this order by the Docker entrypoint:

1. **`mise.toml`** (preferred) — placed in the same directory as `config.yaml`. Declares tools (`[tools]`, any [mise backend](https://mise.jdx.dev/dev-tools/backends/): registry, `pipx:`, `npm:`, `github:`, `cargo:`, `go:`, ...) and Debian packages (`[bootstrap.packages]` with `"apt:<pkg>"` keys). Tools are installed to the `/app/tools` volume and put on `PATH` for every check and action.
2. **`data/setup.sh`** — escape hatch for anything mise cannot express. Runs after mise (so it can use the installed tools) on every container start, so commands must be idempotent.

Failure of either step is non-fatal: the app still starts and affected checks report errors.

For non-Docker deployments, install tools directly on the host.

See Module 7 (§7.4, `docker/entrypoint.sh`) for implementation details and design rationale.

## 4.6 — Check Scheduling

**File:** `src/grimoire/checks/scheduler.py`

- On startup, load all checks and register enabled ones with APScheduler.
- If `schedule` is set, use a `CronTrigger` parsed from the cron expression.
- If `schedule` is not set, the check runs after each data refresh (driven by `refresh_schedule`).
- Toggling a check on/off adds/removes it from the scheduler and updates `CheckToggleRecord` in the DB.
- Toggle state persists across restarts (read from DB on startup).

## 4.7 — Check REST API

**File:** `src/grimoire/checks/router.py`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/checks` | List all check definitions + enabled status |
| `GET` | `/api/checks/{slug}/results` | Latest results for a check, grouped by repo+branch |
| `GET` | `/api/checks/{slug}/runs` | List run history (reverse chronological) |
| `GET` | `/api/checks/{slug}/runs/{run_id}` | Get a specific run with per-repo results |
| `GET` | `/api/checks/{slug}/status` | Check if currently running: `{"slug": ..., "running": bool}` |
| `POST` | `/api/checks/{slug}/run` | Trigger a check run. Optional query: `?repo=owner/repo` to target a single repo |
| `POST` | `/api/checks/{slug}/toggle` | Toggle check enabled/disabled |

**Response models** (Pydantic, for auto-generated OpenAPI docs):

```python
class CheckListItem(BaseModel):
    name: str
    slug: str
    description: str
    schedule: str | None
    enabled: bool
    target_count: int  # number of repos this check applies to

class CheckResultResponse(BaseModel):
    check_name: str
    repo_full_name: str
    branch: str
    passed: bool
    output: str
    timestamp: datetime

class CheckRepoResultResponse(BaseModel):
    repo_full_name: str
    branch: str
    passed: bool
    output: str

class CheckRunSummary(BaseModel):
    id: int
    check_slug: str
    check_name: str
    triggered_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    total_repos: int
    passed_repos: int

class CheckRunDetail(BaseModel):
    id: int
    check_slug: str
    check_name: str
    triggered_by: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    results: list[CheckRepoResultResponse]
```

## Acceptance Criteria

- [ ] YAML check definitions are loaded and validated correctly
- [ ] Invalid YAML (missing fields, multiple target types) raises clear errors at startup
- [ ] Target resolution works for all three modes (list, regex, script)
- [ ] Check scripts run in the correct workdir with correct env vars
- [ ] stdout/stderr is captured in the result
- [ ] Timeout kills the subprocess and records failure
- [ ] Results are stored in the database
- [ ] Scheduling works (default interval + custom cron)
- [ ] Toggle persists across restarts (stored in DB)
- [ ] REST API endpoints return correct responses with proper status codes
- [ ] `POST /api/checks/{slug}/run?repo=owner/repo` targets only the specified repo
- [ ] Tests mock subprocess execution and workspace
