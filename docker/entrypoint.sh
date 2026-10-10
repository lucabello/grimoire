#!/bin/sh
set -e

# Failures in the setup steps are non-fatal — the app should still start even
# if tool installation fails; the affected checks will simply report errors.
warn() {
    echo "WARNING: $1"
}

# mise.toml lives next to the config file (GRIMOIRE_CONFIG, like the app).
config_file="${GRIMOIRE_CONFIG:-/app/config.yaml}"
mise_file="$(dirname "$config_file")/mise.toml"
setup_script="${GRIMOIRE_SETUP_SCRIPT:-/app/data/setup.sh}"

# Both mise commands are idempotent and cheap when nothing is missing, so they
# run on every start. No "already done" stamp: apt packages live in the
# container layer while mise tools live on a volume, so a stamp would go stale
# when the container is re-created.
if [ -f "$mise_file" ]; then
    export MISE_GLOBAL_CONFIG_FILE="$mise_file"
    if grep -q '^\[bootstrap\.packages\]' "$mise_file"; then
        echo "Installing system packages from $mise_file ..."
        mise bootstrap packages apply --manager apt \
            || warn "mise system packages failed with code $?"
    fi
    echo "Installing tools from $mise_file ..."
    mise install || warn "mise install failed with code $?"
    # Put tools on PATH directly instead of via shims: checks run inside cloned
    # repos, where a repo-local mise config could interfere with shim lookups.
    bin_paths="$(mise bin-paths 2>/dev/null | tr '\n' ':')" || bin_paths=""
    export PATH="${bin_paths}${PATH}"
fi

# Escape hatch for anything mise can't express. Runs after mise so it can use
# the installed tools.
if [ -f "$setup_script" ]; then
    echo "Running $setup_script ..."
    sh "$setup_script" || warn "$setup_script exited with code $?"
fi

exec "$@"
