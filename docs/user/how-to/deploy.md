# Deploy Grimoire

This guide covers production deployment options for Grimoire.

## Docker Compose

The recommended way to run Grimoire in production:

```yaml
# docker-compose.yml
services:
  grimoire:
    build: .
    # Or use a pre-built image:
    # image: ghcr.io/lucabello/grimoire:latest
    ports:
      - "8000:8000"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./data:/app/data:ro
      - grimoire-workspace:/app/workspace
      - ./state:/app/state
    environment:
      - GITHUB_TOKEN=${GITHUB_TOKEN}
    restart: unless-stopped

volumes:
  grimoire-workspace:
```

### Volume mounts

| Mount | Purpose |
|-------|---------|
| `config.yaml` | Configuration file (read-only) |
| `data/` | Check and action definitions (read-only) |
| `workspace/` | Cloned repos (managed by Grimoire) |
| `state/` | Database and logs (persistent) |

## Plain Docker

```bash
docker build -t grimoire .

docker run -d \
  --name grimoire \
  -p 8000:8000 \
  -v ./config.yaml:/app/config.yaml:ro \
  -v ./data:/app/data:ro \
  -v grimoire-workspace:/app/workspace \
  --restart unless-stopped \
  grimoire
```

## Reverse proxy

Grimoire listens on port 8000. Place it behind a reverse proxy for TLS termination.

### Caddy

```
grimoire.example.com {
    reverse_proxy localhost:8000
}
```

### Nginx

```nginx
server {
    listen 443 ssl;
    server_name grimoire.example.com;

    ssl_certificate /etc/letsencrypt/live/grimoire.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/grimoire.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Serving under a path prefix (ingress)

To expose Grimoire at a sub-path such as `https://example.com/grimoire/`, start it with `--root-path`:

```bash
grimoire --root-path /grimoire
# or, when invoking uvicorn directly (e.g. in the Docker image):
uvicorn grimoire.app:create_app --factory --host 0.0.0.0 --port 8000 --root-path /grimoire
```

All links, HTMX requests and static assets are then generated under `/grimoire`. The ingress may either strip the prefix before forwarding or forward the path unchanged — both work. For example, a Kubernetes ingress without rewriting:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grimoire
spec:
  rules:
    - http:
        paths:
          - path: /grimoire
            pathType: Prefix
            backend:
              service:
                name: grimoire
                port:
                  number: 8000
```

Health and metrics endpoints are served under the prefix too (e.g. `/grimoire/health`); probes that hit the pod directly can keep using `/health`.

## Health checks

Grimoire exposes a health endpoint at `GET /health`. The Docker image includes a built-in `HEALTHCHECK` that polls this endpoint every 30 seconds.

## Security considerations

!!! warning "Grimoire executes arbitrary shell scripts"

    Checks and actions run user-provided bash scripts with full system access.
    Never expose Grimoire to untrusted users. Place it behind authentication
    if publicly accessible, and use a dedicated GitHub token with minimal permissions.

## Resource usage

Grimoire is lightweight:

| Resource | Usage |
|----------|-------|
| CPU | Minimal; spikes during check/action execution |
| Memory | ~50–100 MB base; scales with tracked repos |
| Disk | Database grows slowly; workspace size depends on cloned repos |
| Network | GitHub API calls minimised via ETags and conditional requests |
