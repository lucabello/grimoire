FROM python:3.13-slim AS base

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

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install mise, used by docker/entrypoint.sh to install the tools declared in
# mise.toml (next to config.yaml). Tools live on a volume so they survive
# container re-creation.
ARG MISE_VERSION=v2026.10.7
RUN curl -fsSL https://mise.run \
    | MISE_VERSION="${MISE_VERSION}" MISE_INSTALL_PATH=/usr/local/bin/mise sh
ENV MISE_DATA_DIR=/app/tools \
    MISE_YES=1
VOLUME /app/tools

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uv", "run", "uvicorn", "grimoire.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
