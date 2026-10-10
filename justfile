set shell := ["bash", "-c"]

export UV_FROZEN := "true"

# List available commands
[private]
@default:
    just --list
    echo ""
    echo "For help with a specific recipe, run: just --usage <recipe>"

# ============================================================================
# Run
# ============================================================================

# Run the application in production mode
[group("run")]
run:
    uv run uvicorn grimoire.app:create_app --factory --loop asyncio --port 8000

# Run the Docker container
[group("run")]
docker-run:
    docker run -p 8000:8000 \
      -e GRIMOIRE_CONFIG=/app/config/config.yaml \
      -v ./config:/app/config:ro \
      -v ./data:/app/data:ro \
      -v grimoire-tools:/app/tools \
      grimoire

# ============================================================================
# Development
# ============================================================================

# Run the application in development mode (with auto-reload)
[group("dev")]
dev:
    uv run uvicorn grimoire.app:create_app --factory --reload --reload-dir src --reload-dir data --reload-include "*.yaml" --loop asyncio --port 8000

# Run all quality checks
[group("dev")]
check: format lint test
    @echo; echo "✓ All checks passed!"

# Format the codebase
[group("dev")]
format:
    # Fix generic linting issues
    uv run ruff check --fix-only
    # Fix import-related issues (including ordering)
    uv run ruff check --select=I --fix-only
    # Format the code
    uv run ruff format

# Lint the codebase
[group("dev")]
lint:
    # Lint the code
    uv run ruff check
    # Run static checks
    uv run pyright src
    # Check for misspellings
    uv run codespell src
    # Check for dead code
    uv run vulture src
    # Run actionlint on GitHub Actions workflows
    test -d .github/workflows && uv run actionlint || true
    @echo "✓ Linting passed!"


# Run tests
[group("dev")]
test:
    uv run pytest

# Run tests with coverage information
[group("dev")]
coverage:
    uv run pytest --cov=src --cov-report=term-missing

# Sync and activate the virtual environment (.venv)
[group("dev")]
venv:
    @echo "Activating virtual environment..."
    @uv sync --all-groups --all-extras
    @. .venv/bin/activate; exec "$SHELL" -i

# ============================================================================
# Documentation
# ============================================================================

# Serve documentation locally (live reload)
[group("docs")]
docs:
    uv run --group docs mkdocs serve --config-file docs/user/mkdocs.yml --dev-addr localhost:8100

# Build documentation site
[group("docs")]
docs-build:
    uv run --group docs mkdocs build --config-file docs/user/mkdocs.yml

# ============================================================================
# Build & Package
# ============================================================================

# Build the project
[group("build")]
build:
    uv build

# Build the Docker image
[group("build")]
docker-build:
    docker build -t grimoire .

# Remove build artifacts and temporary files
[group("build")]
clean:
    # Remove __pycache__ directories
    find . -type d -name "__pycache__" -exec rm -r {} + || true
    # Remove .ruff_cache directory
    rm -rf .ruff_cache
    # Remove .pytest_cache directory
    rm -rf .pytest_cache
    # Remove build/dist/egg-info directories
    rm -rf build dist *.egg-info
    # Remove coverage reports
    rm -f .coverage coverage.xml

# ============================================================================
# Maintenance
# ============================================================================

# Update this justfile from the blueprint repository
[group("maintenance")]
[confirm("Fetch the justfile from *lucabello/blueprints* ? (y/n):")]
@refresh:
    echo "Fetching latest justfile from blueprints repository..."
    gh api repos/lucabello/blueprints/contents/blueprints/python/justfile --jq '.content' | base64 --decode > justfile
    echo "✓ justfile updated"

# Update uv.lock dependencies
[group("maintenance")]
lock:
    unset UV_FROZEN; uv lock --upgrade --no-cache

# Scan for security vulnerabilities
[group("maintenance")]
scan:
    @echo "Scanning for security vulnerabilities..."
    uv run uv-secure

# ============================================================================
# Version & Release
# ============================================================================

# Bump the version in pyproject.toml
[group("release")]
[arg("level", long="level", pattern="^(major|minor|patch)$", help="Semantic version bump level")]
bump level:
    #!/usr/bin/env bash
    uv version --bump={{level}}

# Publish to PyPI (requires authentication)
[group("release")]
[arg("test", long="test", help="Publish to TestPyPI")]
publish test="false": clean build
    #!/usr/bin/env bash
    if [ "{{test}}" = "true" ]; then
        uv publish --index testpypi
    else
        uv publish
    fi

# Publish Docker image to DockerHub (requires authentication)
[group("release")]
publish-docker:
    #!/usr/bin/env bash
    version="$(yq -oy .project.version pyproject.toml)"
    echo "Building and publishing grimoire:${version} to DockerHub..."
    docker build -t lucabello/grimoire:"${version}" -t lucabello/grimoire:latest .
    docker push lucabello/grimoire:"${version}"
    docker push lucabello/grimoire:latest
    echo "✓ Published lucabello/grimoire:${version} and lucabello/grimoire:latest"

# Create a GitHub release and publish to PyPI and DockerHub
[group("release")]
release: (publish "false") publish-docker
    #!/usr/bin/env bash
    latest_release="$(gh release list --limit=1 --order=desc --json=tagName | jq -r '.[].tagName')"
    echo "Latest release on GitHub is ${latest_release}"
    pyproject_release="$(yq -oy .project.version pyproject.toml)"
    echo "Current version in pyproject.toml is v${pyproject_release}"
    if [[ "${latest_release}" == "v${pyproject_release}" ]] ; then
        echo "Error: Latest release version matches pyproject.toml version. Bump the version first by using 'just bump'."
        exit 1
    fi
    read -p "Proceed releasing v${pyproject_release}? (y/n): " answer
    if [[ ! "$answer" == [Yy] ]] ; then
        echo "Cancelled."
        exit 1
    fi
    gh release create "v${pyproject_release}" --generate-notes --notes-start-tag="${latest_release}"
