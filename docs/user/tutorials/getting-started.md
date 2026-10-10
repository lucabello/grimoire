# Getting started

This tutorial walks you through installing Grimoire and monitoring your first repositories.

## Before you begin

You need:

- A **GitHub personal access token** with `repo` scope (read access to the repos you want to monitor)
- Python 3.13+ with [uv](https://docs.astral.sh/uv/) — or **Docker** if you prefer containers

## Step 1 — Clone and configure

```bash
git clone https://github.com/lucabello/grimoire.git
cd grimoire
cp examples/config.yaml config.yaml
```

Open `config.yaml` and set your token and the repositories you want to monitor:

```yaml
github:
  token: "${GITHUB_TOKEN}"   # or a literal token

repositories:
  - repo: "your-org/your-repo"
```

## Step 2 — Start Grimoire

=== "uv / pipx"

    Install as a tool:

    ```bash
    # Using uv
    uv tool install grimoire-dashboard

    # Or using pipx
    pipx install grimoire-dashboard

    # Or run directly without installing
    uvx --from grimoire-dashboard grimoire
    ```

=== "Local (with just)"

    The quickest way to run Grimoire locally — no container required:

    ```bash
    just run
    ```

    This installs dependencies (via `uv`) and starts the server on port 8000.

=== "Docker Compose"

    For a containerised deployment. The bundled `docker-compose.yml` reads its configuration from a `config/` directory, so move the file there first:

    ```bash
    mkdir -p config && mv config.yaml config/
    export GITHUB_TOKEN="ghp_your_token_here"
    docker compose up -d
    ```

## Step 3 — Open the dashboard

Navigate to [http://localhost:8000](http://localhost:8000).

Grimoire fetches data from the GitHub API immediately on startup. The dashboard populates as data arrives — each repository card shows a `fetched_at` timestamp so you know how fresh the data is.

## Step 4 — Explore the interface

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Sortable overview of all repos' health |
| Backlog | `/backlog` | Prioritised list of problems across repos |
| Checks | `/checks` | Custom validation scripts and their results |
| Actions | `/actions` | Automated remediation tasks and run logs |
| API docs | `/docs` | Interactive Swagger UI |

## What's next

- [Write your first check](../how-to/write-a-check.md) to enforce a standard across repos
- Read the [configuration reference](../reference/configuration.md) for all available options
- [Deploy to production](../how-to/deploy.md) with Docker and a reverse proxy
