# Module 7: Observability & DevOps

Add Prometheus metrics, structured logging, Docker packaging, and finalize the justfile and API documentation.

**Dependencies:** Modules 1–6 (instruments the running application).

## 7.1 — Health Endpoint

**Endpoint:** `GET /health`

Returns application health status for Docker HEALTHCHECK and monitoring tools.

```python
class HealthResponse(BaseModel):
    status: str                    # "ok" | "degraded"
    cache_age_seconds: int | None  # seconds since last successful data refresh
    rate_limit_remaining: int | None
    version: str

# Returns 200 for "ok", 200 for "degraded" (with details), 503 if unresponsive
```

The endpoint is `"degraded"` when: rate limit is low (< 10%), cached data is older than two cron intervals (computed from `refresh_schedule`), or the last refresh failed.

## 7.2 — Prometheus Metrics

**File:** `src/grimoire/observability/metrics.py`
**Endpoint:** `GET /metrics`

All metrics are documented in [`METRICS.md`](../METRICS.md) at the repository root. That file is the sole source of truth for metric names, types, labels, and descriptions.

### Implementation

Use `prometheus_client` library:

```python
from prometheus_client import Gauge, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Define metrics at module level
REPOS_TOTAL = Gauge("grimoire_repositories_total", "Number of tracked repos")
OPEN_ISSUES = Gauge("grimoire_open_issues_total", "Open issues", ["repo"])
# ...

# Expose via FastAPI route
@router.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

Metrics are updated:
- After each data refresh cycle (repo health gauges).
- After each check/action execution (duration histograms).
- After each GitHub API call (request counter, rate limit gauge).

## 7.3 — Structured Logging

**File:** `src/grimoire/observability/logging.py`

### Setup

Configure the OpenTelemetry logging SDK to emit structured JSON logs:

- Output to the configured `log_file` path (from `config.yaml`).
- Also output to **stdout** (for Docker/container environments).
- Use the OpenTelemetry Python SDK's `LoggingHandler` to bridge Python's `logging` module.

### Log format

JSON, one object per line:

```json
{
  "timestamp": "2025-04-10T14:30:00.000Z",
  "level": "INFO",
  "message": "Check completed",
  "module": "grimoire.checks.engine",
  "attributes": {
    "check": "uv-lock-fresh",
    "repo": "lucabello/grimoire",
    "branch": "main",
    "passed": true,
    "duration_ms": 1234
  },
  "trace_id": "abc123...",
  "span_id": "def456..."
}
```

### FastAPI instrumentation

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

FastAPIInstrumentor.instrument_app(app)
```

This automatically creates spans for each HTTP request, which are referenced in log entries via `trace_id` and `span_id`.

### Log levels used

| Level | When |
|-------|------|
| `DEBUG` | API response details, cache hits/misses |
| `INFO` | Check/action completed, data refresh completed, startup/shutdown |
| `WARNING` | Rate limit approaching, API call failed (using cache), stale data |
| `ERROR` | Persistent API failure, check/action timeout, config error |
| `CRITICAL` | Application startup failure, database corruption |

## 7.4 — Dockerfile

```dockerfile
FROM python:3.13-slim AS base

# Install system dependencies needed by checks/actions
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gpg \
    ssh-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install gh CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install mise (pinned); tools live on a volume so they survive re-creation
ARG MISE_VERSION=v2026.10.7
RUN curl -fsSL https://mise.run \
    | MISE_VERSION="${MISE_VERSION}" MISE_INSTALL_PATH=/usr/local/bin/mise sh
ENV MISE_DATA_DIR=/app/tools MISE_YES=1
VOLUME /app/tools

WORKDIR /app

# Install dependencies (cached layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --frozen

# Copy application
COPY src/ src/
COPY docker-entrypoint.sh /usr/local/bin/
COPY config.yaml.example ./

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "grimoire.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

### `docker-entrypoint.sh`

The entrypoint installs the external tools needed by checks and actions before starting the application. Two mechanisms, in order:

1. **`mise.toml`** next to the config file (`dirname $GRIMOIRE_CONFIG`, default `/app/config.yaml`). If present:
   - `MISE_GLOBAL_CONFIG_FILE` is exported to point at it.
   - If it contains a `[bootstrap.packages]` table: `mise bootstrap packages apply --manager apt` (Debian packages; the container runs as root).
   - `mise install` (tools).
   - `mise bin-paths` is prepended to `PATH`.
2. **`data/setup.sh`** (override with `GRIMOIRE_SETUP_SCRIPT`), run after mise so it can use the installed tools.

Then `exec "$@"`. A failure in any step prints a `WARNING` and continues — the app still starts and affected checks report errors.

Example `mise.toml` (see `mise.toml.example`):

```toml
[tools]
jq = "latest"
"pipx:charmcraft" = "latest"

[bootstrap.packages]
"apt:libpq5" = "latest"
```

#### Design decisions

- **mise over per-ecosystem config keys.** One declarative file covers registry tools, pip/npm/cargo/go packages and GitHub release binaries via mise backends, so Grimoire does not need to know about any package manager. Snaps are deliberately unsupported (snapd does not run in containers); use the PyPI/release-binary distribution or `setup.sh` (e.g. `unsquashfs`) instead.
- **No "already installed" stamp.** `mise install` and `mise bootstrap packages apply` are idempotent and near-instant when nothing is missing. A stamp stored on the tools volume would go stale when the container is re-created, because apt packages live in the container layer while mise tools live on the volume.
- **`PATH` entries instead of shims.** Checks run inside cloned repositories; a repo-local mise config could make shim lookups fail or prompt for trust. `mise bin-paths` is deterministic.
- **`mise.toml` beside `config.yaml`, not in `data/`.** It is deployment configuration, not a check/action definition. The entrypoint honours `GRIMOIRE_CONFIG` only (not the `--config-file` CLI flag, which it cannot see). With the legacy single-file mount (`/app/config.yaml`) the sibling directory is `/app`, so mise is simply skipped.
- **`[bootstrap.packages]` is mise's newer, still-evolving apt integration** (earlier docs call it `[system.packages]`). The mise version is therefore pinned via `MISE_VERSION`; bump it deliberately.

### `.dockerignore`

```
workspace/
.venv/
.git/
*.db
*.log
__pycache__/
.ruff_cache/
.pytest_cache/
.coverage
dist/
build/
```

### Volumes

| Mount point | Purpose |
|-------------|---------|
| `/app/config/` | `config.yaml` and optional `mise.toml` (bind mount; set `GRIMOIRE_CONFIG=/app/config/config.yaml`) |
| `/app/data/` | Check and action YAML definitions, optional `setup.sh` |
| `/app/tools/` | mise-installed tools and cache (named volume) |
| `/app/workspace/` | Cloned repository working directories |
| `/app/state/` | Persistent state: SQLite database + log file |
| `/keys/` | SSH/GPG keys for signing |

### Docker Compose example

```yaml
services:
  grimoire:
    build: .
    ports:
      - "8000:8000"
    environment:
      GRIMOIRE_CONFIG: /app/config/config.yaml
    volumes:
      - ./config:/app/config:ro      # config.yaml + optional mise.toml
      - ./data:/app/data:ro
      - grimoire-tools:/app/tools
      - grimoire-workspace:/app/workspace
      - grimoire-state:/app/state
      - ~/.ssh/id_ed25519:/keys/id_ed25519:ro
      - ~/.ssh/known_hosts:/keys/known_hosts:ro

volumes:
  grimoire-tools:
  grimoire-workspace:
  grimoire-state:    # contains grimoire.db + grimoire.log
```

**Note:** Configure `database_path: /app/state/grimoire.db` and `log_file: /app/state/grimoire.log` in `config.yaml` when using Docker.

## 7.5 — justfile Updates

Add/update recipes in the existing `justfile`:

```just
# Run the application in development mode (with auto-reload)
[group("dev")]
dev:
    uv run uvicorn grimoire.app:create_app --factory --reload --port 8000

# Run the application in production mode
[group("dev")]
run:
    uv run uvicorn grimoire.app:create_app --factory --port 8000

# Build the Docker image
[group("build")]
docker-build:
    docker build -t grimoire .

# Run the Docker container
[group("build")]
docker-run:
    docker run -p 8000:8000 \
      -e GRIMOIRE_CONFIG=/app/config/config.yaml \
      -v ./config:/app/config:ro \
      -v ./data:/app/data:ro \
      -v grimoire-tools:/app/tools \
      grimoire
```

## 7.6 — API Documentation

FastAPI auto-generates OpenAPI docs at `/docs` (Swagger UI) and `/redoc`.

Ensure all API routes have:
- Descriptive `summary` and `description` strings.
- Typed request/response Pydantic models with `Field(description=...)` and `json_schema_extra` for examples.
- Proper HTTP status codes and error response models.
- Tags for grouping (e.g., `tags=["checks"]`, `tags=["actions"]`, `tags=["repositories"]`).

## Acceptance Criteria

- [ ] `GET /health` returns status, cache age, and rate limit info
- [ ] Health reports "degraded" when cache is stale or rate limit is low
- [ ] Docker HEALTHCHECK uses `/health` endpoint
- [ ] `.dockerignore` excludes workspace, .venv, .git, etc.
- [ ] `GET /metrics` returns valid Prometheus text format with all defined metrics
- [ ] Metrics are updated correctly after data refreshes, check runs, and action runs
- [ ] Structured JSON logs are written to the configured log file
- [ ] Logs are also emitted to stdout
- [ ] Log entries include trace_id and span_id when within a request context
- [ ] FastAPI requests produce spans via OpenTelemetry instrumentation
- [ ] Docker image builds successfully
- [ ] Tools and `[bootstrap.packages]` from a `mise.toml` next to the config file are installed on start and available to checks
- [ ] Re-starting with a populated tools volume does not re-download mise tools
- [ ] `data/setup.sh` still runs (after mise), and failures in mise or the script do not stop the app starting
- [ ] Without a `mise.toml`, mise is never invoked (legacy deployments unchanged)
- [ ] Container starts and serves the dashboard with all functionality
- [ ] `just dev` and `just run` work correctly
- [ ] `just docker-build` and `just docker-run` work correctly
- [ ] `/docs` shows complete API documentation with descriptions and examples
- [ ] `/redoc` renders correctly as an alternative docs view
