# Module 5: Actions Engine

Load user-defined actions from YAML files, execute them with full logging, maintain run history, schedule optional cron runs, and expose REST APIs.

**Dependencies:** Module 1 (config, models, database), Module 3 (workspace), Module 4 (shared `TargetSpec` model and `resolve_targets` utility from `src/grimoire/targeting.py`).

## 5.1 — Action Definition Format

Actions live as YAML files in `{data_dir}/actions/`. Each file defines one action.

```yaml
# data/actions/update-uv-lock.yaml
# The filename (without .yaml) becomes the URL slug: "update-uv-lock"
name: "Update UV Lock"
description: "Updates uv.lock and opens a PR to main"
targets:
  regex: "lucabello/.*"
  # list: [...]
  # script: "..."
script: |
  uv lock
  BRANCH="chore/update-uv-lock"
  git checkout -b "$BRANCH" || git checkout "$BRANCH"
  git add uv.lock
  git commit -m "chore: update uv.lock" || true
  git push origin "$BRANCH" --force
  gh pr create --title "chore: update uv.lock" --body "Automated lockfile update" || true
schedule: "0 0 * * 1"   # optional cron; omit for manual-only
```

Scripts run via `/bin/sh` by default. Add a shebang to use a different interpreter (bash, python3, etc.):
```yaml
script: |
  #!/usr/bin/env python3
  import subprocess, json
  result = subprocess.run(["gh", "pr", "list", "--json", "number"], capture_output=True, text=True)
  prs = json.loads(result.stdout)
  print(f"Found {len(prs)} open PRs")
```

A minimal action for testing workspace setup:

```yaml
# data/actions/print-workdir.yaml
name: "Print Workdir"
description: "Runs pwd in each repository workspace to verify workspace setup"
targets:
  regex: ".*"
script: |
  pwd
```

A **global** action (no targets — runs once, not per-repo):

```yaml
# data/actions/example-global.yaml
name: "Example Global Action"
description: "Runs a script once, not per repository"
script: |
  echo "Running globally..."
schedule: "0 */3 * * *"
# Note: no "targets" field — this makes it a global action
```

### Pydantic model

```python
class ActionDefinition(BaseModel):
    name: str
    slug: str               # auto-derived from YAML filename (e.g., "update-uv-lock")
    description: str
    targets: TargetSpec | None = None  # None = global (run once, not per-repo)
    script: str
    schedule: str | None = None
    enabled: bool = True
```

**Global actions:** When `targets` is omitted, the action runs its script once (not per-repo). The script executes in the workspace root directory with `GH_TOKEN` and other environment variables available. No `sync_repo()` or `reset_workdir()` is called. Results are stored with `repo_full_name="(global)"`.

**Note:** Actions with a `schedule` have an `enabled` toggle (like checks). Toggling disables/enables the cron schedule. The toggle state is persisted in the `action_toggle` table and restored on startup. Manual-only actions (no schedule) do not show a toggle button.

## 5.2 — Action Loader

**File:** `src/grimoire/actions/loader.py`

```python
def load_actions(data_dir: Path) -> list[ActionDefinition]:
    """
    Load all YAML files from {data_dir}/actions/.
    Parse each into an ActionDefinition model.
    Derive slug from filename (e.g., "update-uv-lock.yaml" → "update-uv-lock").
    Validate slug uniqueness.
    Raise on validation errors (fail fast at startup).
    """
```

## 5.3 — Action Execution Engine

**File:** `src/grimoire/actions/engine.py`

```python
async def run_action(
    action: ActionDefinition,
    repos: list[TrackedRepository],
    workspace: WorkspaceManager,
    triggered_by: str,           # "manual" | "cron" | "api"
    specific_repo: str | None = None,  # if set, only run on this repo
) -> ActionRun:
    """
    1. Check if this action is already running. If so, raise/return 409 Conflict.
    2. Create an ActionRunRecord in the database (status: "running").
    3. If global action (targets is None):
       a. Run script once in workspace root directory.
       b. Record single ActionRunRepoRecord with repo_full_name="(global)".
    4. If per-repo action:
       a. Resolve targets (or filter to specific_repo if provided) → list of
          (repo, matched_branches).
       b. For each (repo, branch) pair (sequentially):
          - Call workspace.sync_repo() to fetch latest from remote.
          - Call workspace.reset_workdir() to ensure clean state.
          - Run action.script as subprocess in workdir.
          - Capture full stdout+stderr (cap at 64KB).
          - Record ActionRunRepoRecord (passed, output).
    5. Update ActionRunRecord (status: "completed", finished_at).
    6. Return ActionRun summary.
    """
```

**Global actions:** When `action.targets is None`, the script runs once in the workspace root directory. No `sync_repo()` or `reset_workdir()` is called. The `specific_repo` parameter is rejected with HTTP 400 for global actions.

**Sequential execution:** Per-repo actions run repos **sequentially**, not concurrently. Actions may have side effects (commits, PRs, branch operations) that could conflict if run in parallel. This is a deliberate safety choice.

**Concurrent run guard:** If an action is already running (an `ActionRunRecord` with `status="running"` exists for this action), reject the new run with HTTP 409 Conflict: `"Action '{slug}' is already running (run ID: {id})"`. This applies to all triggers (manual, cron, API).

**Progress tracking:** Like checks, actions maintain an in-memory `ActionProgress(completed, total)` dataclass in `_running_actions: dict[str, ActionProgress]`. The total is computed as the number of repo×branch tasks before execution begins. Each completed task increments `completed`. The entry is removed in a `finally` block. Helper functions: `is_action_running(slug)`, `get_action_progress(slug)`. The web UI polls this to display a `completed/total` counter on the action run button.

**Subprocess execution details:**
- Same as checks: scripts with a shebang (`#!`) are written to a temp file and run directly (honoring the interpreter); scripts without a shebang are passed to `/bin/sh`. Set `cwd`, and build the env with `target_env(workspace, repo, branch)` (workspace env vars + `REPO_OWNER`/`REPO_NAME`/`REPO_FULL_NAME`/`BRANCH`/`DEFAULT_BRANCH`).
- Capture stdout and stderr together (combined stream).
- Apply a timeout (configurable, default 10 minutes per action per repo — longer than checks, since actions may involve network operations like `git push`).
- On timeout, kill the process, record as failure.
- **Output size cap:** Same as checks — 64KB max per repo, truncate with note if exceeded.

**Branch scoping.** Actions follow the same targeting rules as checks (see spec 04 §4.3): `list`/`regex` include every observed branch of a matched repo, while `script` targeting is evaluated per (repo, branch) and can restrict execution to a subset (e.g. `[ "$BRANCH" = "$DEFAULT_BRANCH" ]` for default-branch-only actions like PR-opening bots).

**Pre-execution sequence** (per repo+branch):
1. `workspace.sync_repo()` — fetch latest from remote (actions need fresh code).
2. `workspace.reset_workdir()` — clean state: correct branch, discard local changes, remove untracked files.

## 5.4 — Action Scheduling

**File:** `src/grimoire/actions/scheduler.py`

- On startup, register actions that have a `schedule` field with APScheduler using `CronTrigger`.
- Cron-triggered runs set `triggered_by: "cron"`.
- Actions without a `schedule` are manual-only (triggered via the web UI or REST API).

## 5.5 — Action REST API

**File:** `src/grimoire/actions/router.py`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/actions` | List all action definitions |
| `GET` | `/api/actions/{slug}/runs` | List run history for an action (paginated, reverse chronological) |
| `GET` | `/api/actions/{slug}/runs/{run_id}` | Get a specific run with per-repo results + logs |
| `GET` | `/api/actions/{slug}/status` | Check if currently running: `{"slug": ..., "running": bool}` |
| `POST` | `/api/actions/{slug}/run` | Trigger an action. Optional query: `?repo=owner/repo`. Returns 409 if already running |

**Response models:**

```python
class ActionListItem(BaseModel):
    name: str
    slug: str
    description: str
    schedule: str | None
    target_count: int

class ActionRunSummary(BaseModel):
    id: int
    action_name: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    total_repos: int
    passed_repos: int

class ActionRepoResultResponse(BaseModel):
    repo_full_name: str
    branch: str
    passed: bool
    output: str

class ActionRunDetail(BaseModel):
    id: int
    action_name: str
    triggered_by: str
    started_at: datetime
    finished_at: datetime | None
    results: list[ActionRepoResultResponse]
```

## Checks vs. Actions — Key Differences

| Aspect | Checks | Actions |
|--------|--------|---------|
| Purpose | Read-only verification | Side-effecting operations |
| Execution | Concurrent across repos | Sequential across repos |
| Default timeout | 5 minutes | 10 minutes |
| Scheduling | Default: refresh interval | Only if explicit `schedule` set |
| Toggle | Can be enabled/disabled | No toggle (remove file to disable) |
| Dashboard | Shown as status badges | Separate Actions page |
| Pre-execution | `reset_workdir()` only | `sync_repo()` + `reset_workdir()` |
| History | Latest result per repo | Full run history with logs |

## Acceptance Criteria

- [ ] YAML action definitions are loaded and validated correctly
- [ ] Actions run sequentially across target repos
- [ ] Full stdout/stderr is captured and stored per repo per run
- [ ] Run history is persisted in the database with correct metadata
- [ ] `triggered_by` is correctly set for manual, cron, and API triggers
- [ ] Cron-scheduled runs produce entries identical in structure to manual runs
- [ ] `POST /api/actions/{slug}/run?repo=owner/repo` targets only the specified repo
- [ ] Concurrent run of the same action is rejected with 409 Conflict
- [ ] REST API endpoints return correct paginated responses
- [ ] Repo is synced (pulled) before action execution
- [ ] Timeout kills the subprocess and records failure
- [ ] Tests mock subprocess execution
