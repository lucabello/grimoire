# AGENTS.md

Instructions for AI agents working on this codebase.

## Orientation

Read `docs/specs/00-overview.md` first — it has the tech stack, project structure, module dependency graph, config format, and REST API summary.

## Specs

Every module has a spec in `docs/specs/`, numbered by dependency order. Each spec is the authoritative reference for its module: it includes all models, file paths, API routes, and acceptance criteria.

- **Read the relevant spec before modifying a module.**
- When adding a feature that spans modules, update all affected specs.
- If a new independent module is needed, create a new numbered spec following the same format.
- Specs are self-contained, agent-optimized, and ordered so module N only depends on modules < N.

## Workflow for new features

Before implementing a new feature, identify which modules and specs are affected. Present this list to the user for approval before writing any code. This includes:

- Which spec files will be read and potentially updated.
- Which source modules will be modified or created.
- Whether new tests are needed and where they belong.

## Commands

Run `just --list` to see all available recipes. The key one is:

```bash
just check    # format + lint + test — run this after every change
```

After any code change, run `just check` and verify zero failures before considering the change complete.

## Commits

All commits must use [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<optional scope>): <description>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `test`, `build`, `ci`, `chore`.

## Testing

Tests mirror the source structure under `tests/`. Key conventions:

- All tests are async — no manual `@pytest.mark.asyncio` needed.
- GitHub API mocking uses `respx` — never make real API calls in tests.
- Web route tests populate the in-memory cache via `update_cache()` in a fixture.
- Filesystem-touching tests use `tmp_path` for isolation.

When modifying or adding functionality, add or update corresponding tests in the matching `tests/` subdirectory.

## Database

The SQLite schema is versioned with Alembic (`src/grimoire/migrations/`) and migrated at startup by `upgrade_database`, so user databases keep their toggles, run history and cache across upgrades. Never ask users to delete `grimoire.db`.

When changing the schema (adding/removing columns or tables):

1. Update the SQLModel definitions in `src/grimoire/database.py`.
2. Run `just migration "short description"` to autogenerate a revision in `src/grimoire/migrations/versions/`.
3. Review the generated revision (SQLite needs batch operations for most alterations; make sure the downgrade works) and commit it together with the model change.

`tests/test_migrations/test_revisions.py` fails when the migrated schema differs from the models, so a model change without a revision does not pass `just check`. Tests create their database with `upgrade_database`.

## Style

- Keep code simple, modular, and testable.
- Only comment where behavior is non-obvious.
- Use `TYPE_CHECKING` imports to avoid circular dependencies between modules.
- Module-level mutable state uses setter functions (e.g., `set_checks_state()`) for dependency injection and testability.

## Documentation

User-facing documentation lives in `docs/user/` and is built with [MkDocs Material](https://squidfunnel.github.io/mkdocs-material/).

- **When adding or changing user-visible features, update the relevant page(s) in `docs/user/`.**
- Configuration changes → update `docs/user/configuration.md`.
- New check/action capabilities → update `docs/user/checks.md` or `docs/user/actions.md`.
- Preview locally with `just docs`.

