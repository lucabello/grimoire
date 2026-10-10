# Contributing to Grimoire

Thanks for your interest in contributing! 🔮

## Development Setup

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) — fast Python package manager
- [just](https://github.com/casey/just) — command runner

### Getting Started

```bash
# Clone the repo
git clone https://github.com/lucabello/grimoire.git
cd grimoire

# Install dependencies and activate the venv
just venv

# Copy and edit the config
cp examples/config.yaml config.yaml

# Run in dev mode (auto-reload)
just dev
```

### Useful Commands

```bash
just check      # Run format + lint + test (do this before every commit)
just format     # Auto-format code
just lint       # Run all linters (ruff, pyright, codespell, vulture)
just test       # Run the test suite
just coverage   # Run tests with coverage report
just docs       # Serve documentation locally
```

## Code Style

- **Formatter/Linter**: [Ruff](https://docs.astral.sh/ruff/) (line length: 99)
- **Type checking**: [Pyright](https://github.com/microsoft/pyright)
- Keep code simple, modular, and testable
- Only comment where behavior is non-obvious
- Use `TYPE_CHECKING` imports to avoid circular dependencies

## Testing

Tests live under `tests/`, mirroring the `src/` structure.

- All tests are **async** — no manual `@pytest.mark.asyncio` needed
- GitHub API mocking uses **respx** — never make real API calls
- Use `tmp_path` for filesystem isolation

When adding a feature, add or update tests in the matching `tests/` subdirectory.

## Pull Request Workflow

1. Create a feature branch from `main`
2. Make your changes
3. Run `just check` and ensure all checks pass
4. Push and open a PR against `main`
5. Describe what your change does and link any related issues

## Architecture

Developer specs live in [`docs/specs/`](docs/specs/). Start with [`00-overview.md`](docs/specs/00-overview.md) for the full architecture, module graph, and API reference.
