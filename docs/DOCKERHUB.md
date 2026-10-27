# pihole-sync

Synchronize Pi-hole v6 lists, domain rules, groups, clients, local DNS records, and selected settings from one primary to one or more replicas, using the Pi-hole REST API.

- **Image:** `eldieturner/pihole-sync`
- **Release:** `0.2.2`; `0.2` follows the minor series; `latest` follows releases.
- **Platforms:** `linux/amd64`, `linux/arm64`, `linux/arm/v7`.
- **Source and full documentation:** [GitHub](https://github.com/EldieTurner/pihole-sync).
- **License:** [MIT](https://github.com/EldieTurner/pihole-sync/blob/main/LICENSE).
- **Support:** [GitHub issues](https://github.com/EldieTurner/pihole-sync/issues).

This is an independent community project, not an official Pi-hole component. Pi-hole v5 is not supported. Each Pi-hole must already resolve DNS independently; this service does not install resolvers, configure Unbound, or provide DNS failover.

## Quick start

Run one sync service for your primary/replica set. Back up your Pi-holes before the first sync. Get API credentials for every instance and use matching Pi-hole versions where possible.

```bash
git clone --branch v0.2.2 https://github.com/EldieTurner/pihole-sync.git
cd pihole-sync
mkdir -p data
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit the API addresses in `config.yaml` and passwords in `.env` before starting.

```yaml
# docker-compose.yml
services:
  pihole-sync:
    image: eldieturner/pihole-sync:0.2.2
    container_name: pihole-sync
    restart: unless-stopped
    env_file:
      - path: .env
        format: raw
    ports:
      - "127.0.0.1:8080:8080"
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ./data:/data
```

Requires Docker Compose 2.30+ for the raw environment-file format. The examples use Bash; Windows users can use WSL.

```bash
docker compose pull pihole-sync
docker compose run --rm --no-deps pihole-sync python -m pihole_sync --validate
docker compose up -d
docker compose logs -f --tail=100 pihole-sync
```

Open http://localhost:8080 on the Docker host. The first sync starts shortly after startup and can update replicas immediately. Validation only loads configuration; it does not contact Pi-hole or preview changes.

## Configuration and secrets

```yaml
# config.yaml — replace the example addresses
sync:
  interval_seconds: 300
  reconcile: false
  delete_extra_lists: false
  delete_extra_domains: false
  delete_extra_groups: false
  delete_extra_clients: false
  exclude_config_paths:
    - dhcp
    - webserver
    - files
    - dns.port
    - dns.upstreams
    - dns.listeningMode
    - dns.dnssec

primary:
  host: http://192.168.1.20:80
  password: ${PIHOLE_PRIMARY_PASSWORD}

replicas:
  - name: pihole2
    host: http://192.168.1.21:80
    password: ${PIHOLE_REPLICA_PASSWORD}
```

```dotenv
# .env — literal KEY=value, without surrounding quotes
PIHOLE_PRIMARY_PASSWORD=replace-with-primary-api-password
PIHOLE_REPLICA_PASSWORD=replace-with-replica-api-password
LOG_LEVEL=INFO
```

The YAML references variables from the container environment. Do not append `/api` or `/admin` to a Pi-hole base URL. Use unique, stable replica names. For another replica, add a separate entry and password variable.

Application passwords are supported. Configuration writes with an application password require permission through Pi-hole's `webserver.api.app_sudo`; otherwise disable configuration-writing toggles on that replica. See the [full permissions and configuration reference](https://github.com/EldieTurner/pihole-sync/blob/main/README.md#api-credentials-and-permissions).

Custom exclusion lists replace the defaults. Keep `dhcp`, `webserver`, and `files` when adding paths. Exclude settings controlled by Pi-hole's `FTLCONF_*` Docker variables, because they are read-only through its API.

## docker run alternative

Create the same `.env`, `config.yaml`, and `data/` first:

```bash
docker run -d \
  --name pihole-sync \
  --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 \
  --mount type=bind,source="$(pwd)/config.yaml",target=/config/config.yaml,readonly \
  --mount type=bind,source="$(pwd)/data",target=/data \
  eldieturner/pihole-sync:0.2.2
```

Choose Compose or `docker run`; do not run both against the same Pi-holes/state directory. To override the image's command, include `python -m pihole_sync`, for example `python -m pihole_sync --validate`.

## Operations and safety

- The dashboard and its API have **no authentication**. Keep the loopback port binding or use an authenticated private proxy/tunnel.
- `127.0.0.1` inside a bridge container refers to that container. Use reachable host addresses or a shared Docker network for Pi-hole APIs.
- Use HTTPS for API traffic across untrusted networks and keep TLS verification enabled.
- Scheduled cycles preserve replica-only edits unless `reconcile` is enabled. The dashboard's **Sync now**, `POST /api/sync`, and `--once` **force reconciliation**, regardless of that flag. Deletion still requires explicit opt-in.
- List subscriptions are synchronized; each Pi-hole downloads its own lists and rebuilds gravity. Files, query history, caches, and Unbound configuration are not replicated.
- A failed cycle can leave some remote writes applied. There is no cross-host rollback or dry-run mode.
- Docker's healthcheck tests the dashboard endpoint, not successful synchronization. Check logs and `/api/status` for replica results.
- Preserve `/data` for applied snapshots and history. Stop the service before backing up its entire state directory. Back up Pi-hole independently.
- Restart/recreate the service after changing YAML or credentials. Pin image versions or digests for controlled upgrades.

No privileged mode, Docker socket, SSH keys, or mounted Pi-hole data directories are required. The image includes its license and public reference files at `/usr/share/doc/pihole-sync/`.

For troubleshooting, configuration defaults, source builds, and release notes, see the [README](https://github.com/EldieTurner/pihole-sync/blob/main/README.md) and [changelog](https://github.com/EldieTurner/pihole-sync/blob/main/CHANGELOG.md). Redact passwords, TOTP seeds, internal details, and tokens before sharing diagnostics.
