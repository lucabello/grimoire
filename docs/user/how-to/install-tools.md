# Install tools for your checks

Checks and actions run shell scripts, so they often need tools that are not in the Grimoire Docker image: `jq`, `shellcheck`, `charmcraft`, a specific Node.js version, and so on. In Docker, you declare these in a `mise.toml` file and Grimoire installs them when the container starts.

[mise](https://mise.jdx.dev/) is a tool manager that can install thousands of tools without root and without you writing install commands. Grimoire only runs it; everything mise supports works here.

!!! note "Docker only"

    When you run Grimoire directly with `uv` or `pip`, install tools on the host as usual. `mise.toml` is read only by the Docker image's entrypoint.

## Step 1 — Put `mise.toml` next to `config.yaml`

Grimoire looks for `mise.toml` in the **same directory as your config file**. Mount that directory instead of the single file, and point `GRIMOIRE_CONFIG` at it:

```yaml
# docker-compose.yml
services:
  grimoire:
    image: ghcr.io/lucabello/grimoire:latest
    environment:
      GRIMOIRE_CONFIG: /app/config/config.yaml
    volumes:
      - ./config:/app/config:ro      # config.yaml + mise.toml
      - ./data:/app/data:ro
      - grimoire-tools:/app/tools    # installed tools (cached)
      - grimoire-workspace:/app/workspace

volumes:
  grimoire-tools:
  grimoire-workspace:
```

```
config/
├── config.yaml
└── mise.toml
```

!!! info "Existing deployments keep working"

    If you still mount only `config.yaml` at `/app/config.yaml`, there is no `mise.toml` beside it and Grimoire skips this step. Nothing else changes.

## Step 2 — Declare your tools

```toml
# config/mise.toml
[tools]
jq = "latest"
shellcheck = "0.10"
node = "22"
"pipx:charmcraft" = "latest"
```

Restart the container. On start, Grimoire installs whatever is missing and puts the tools on the `PATH` of every check and action. The first start downloads everything; later starts take a moment because the `grimoire-tools` volume keeps the installs.

A copy of this file with more examples is in the repository as [`mise.toml.example`](https://github.com/lucabello/grimoire/blob/main/mise.toml.example).

## Where tools come from

The key on the left of each `[tools]` entry selects a mise *backend*. The full list is in the [mise backends documentation](https://mise.jdx.dev/dev-tools/backends/); the ones most useful for checks are:

| Entry | Installs | Notes |
|-------|----------|-------|
| `jq = "latest"` | A tool from the [mise registry](https://mise.jdx.dev/registry.html) | Easiest option when the tool is listed |
| `"pipx:yamllint" = "latest"` | A Python CLI from PyPI | Uses `uv`, which the image includes |
| `"npm:prettier" = "latest"` | An npm package | Needs `node` in `[tools]` |
| `"github:owner/repo" = "latest"` | A prebuilt binary from GitHub releases | Good for tools with no registry entry |
| `"cargo:ripgrep" = "latest"` | A Rust crate | Compiled from source: slow on first start |
| `"go:github.com/owner/tool" = "latest"` | A Go module | Compiled from source: slow on first start |

Prefer prebuilt binaries (registry, `github:`) over `cargo:` and `go:` when you have the choice.

## Install system packages (apt)

Some things are not standalone tools, such as shared libraries a Python package needs. For those, declare Debian packages in `[bootstrap.packages]`:

```toml
[bootstrap.packages]
"apt:libpq5" = "latest"
"apt:curl" = "8.14.1-2"   # pin a version from `apt-cache policy curl`
```

This uses mise's [apt integration](https://mise.jdx.dev/bootstrap/packages/apt.html). The Grimoire container runs as root, so no `sudo` is needed.

!!! warning "apt packages are not stored on the tools volume"

    They are installed into the container itself. If you re-create the container (for example when upgrading the image), they are installed again on the next start. `[tools]` entries are not affected.

!!! note "mise's package support is evolving"

    `[bootstrap.packages]` is newer than `[tools]` and its syntax may change. The Grimoire image pins a mise version, so a Grimoire upgrade is the point where this can change. Check the [mise documentation](https://mise.jdx.dev/bootstrap/packages/apt.html) if something in this section stops working.

## Anything else: `setup.sh`

When mise cannot express what you need, put a script at `data/setup.sh`. It runs after the mise step, so it can use tools mise installed.

```sh
# data/setup.sh
wget -qO /usr/local/bin/mytool https://example.com/mytool
chmod +x /usr/local/bin/mytool
```

It runs on **every start**, so every command must be safe to repeat.

## Order of operations

On every container start:

1. `[bootstrap.packages]` from `mise.toml` (apt)
2. `[tools]` from `mise.toml`
3. `data/setup.sh`
4. Grimoire starts

If a step fails, the container prints a `WARNING` in its logs and continues. Grimoire still starts, and checks that need the missing tool fail with a "command not found" error. Look at `docker logs grimoire` first when a check fails unexpectedly.

## Limitations

- **Snaps are not supported.** `snapd` does not run inside containers. Most snap-only tools have another distribution: for example `charmcraft` is on PyPI (`"pipx:charmcraft"`). Otherwise download the release binary, or unpack the `.snap` with `unsquashfs` in `setup.sh`; classic-confinement snaps may not work that way.
- **Changing `mise.toml` needs a restart.** Tools are installed at container start, not while Grimoire runs.
- **Only `GRIMOIRE_CONFIG` is honoured.** The entrypoint cannot see the `--config-file` command-line flag, so set the environment variable when you use a non-default location.
- **Removed tools stay on the volume.** Delete the `grimoire-tools` volume to start clean.
