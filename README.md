# pihole-sync

[![CI](https://github.com/EldieTurner/pihole-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/EldieTurner/pihole-sync/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Synchronize lists, domain rules, groups, clients, and selected settings from one **Pi-hole v6** instance to one or more replicas. A small web dashboard shows results and lets you trigger a sync.

The service uses the Pi-hole REST API. It does not require SSH access, copy Pi-hole databases, or mount Pi-hole's configuration directory.

```text
                         reads configuration
Primary Pi-hole v6  ------------------------------>  pihole-sync
                                                      |      |
                                   applies changes    |      +-- Dashboard
                                                      v
                                            Replica Pi-hole(s)
```

Run **one sync service per primary/replica set**. Make shared configuration changes on the primary. Synchronization is one way; it does not merge edits from multiple primaries.

## Contents

- [What it synchronizes](#what-it-synchronizes)
- [Requirements](#requirements)
- [Quick start with Docker Compose](#quick-start-with-docker-compose)
- [Example config.yaml](#example-configyaml)
- [Example .env](#example-env)
- [Run with docker run](#run-with-docker-run)
- [Validation and one-shot sync](#validation-and-one-shot-sync)
- [Configuration reference](#configuration-reference)
- [Networking and Docker-managed Pi-hole settings](#networking-and-docker-managed-pi-hole-settings)
- [Sync behavior and limitations](#sync-behavior-and-limitations)
- [Monitoring and troubleshooting](#monitoring-and-troubleshooting)
- [Security](#security)
- [Updates, backups, and recovery](#updates-backups-and-recovery)
- [Development and publishing](#development-and-publishing)

## What it synchronizes

| Configuration | Support |
| --- | --- |
| Subscribed block and allow lists | URLs, enabled state, comments, and group assignments |
| Domain allow/deny rules | Exact and regular-expression rules |
| Groups | Matched by name; numeric group references are remapped per replica |
| Managed clients | Client identifiers, comments, and group assignments |
| Local DNS | Host records and CNAME records |
| General Pi-hole settings | Configurable, with exclusions for settings that belong to each instance |
| Static DHCP assignments | Optional; disabled by default |

List subscriptions are synchronized through the API. Each Pi-hole downloads and builds its own gravity database; downloaded list contents and database files are not copied between hosts. Identical subscriptions can briefly have different counts if list publishers update between downloads.

DHCP scope/server settings are not synchronized. Webserver settings and filesystem paths are excluded by default. Credentials and masked secret values are excluded from configuration synchronization.

This project does not install Pi-hole, configure Unbound, replicate DNS caches or query history, change router/client DNS settings, provide a virtual IP, or implement DNS failover. Each resolver must already work independently.

## Requirements

- At least two working **Pi-hole v6** instances, with API access from the sync container. Pi-hole v5 is not supported.
- Keep Pi-hole instances on the same release where practical. Different configuration schemas can cause repeated patches or rejected settings.
- Docker Engine or Docker Desktop. The Compose example requires **Docker Compose 2.30+** for `env_file.format: raw`.
- A credential for every Pi-hole. Separate application passwords are preferable to storing administrator passwords.
- A writable directory for the service's SQLite state.

Pi-hole may run in Docker or directly on a host. The sync container can run on either Pi-hole host or on another machine with access to their APIs.

### API credentials and permissions

Generate an application password in each Pi-hole's web interface under its API settings. Application passwords can authenticate without supplying a rotating 2FA code. See [Pi-hole authentication](https://docs.pi-hole.net/api/auth/).

Application-password sessions cannot modify configuration settings unless that Pi-hole permits it through `webserver.api.app_sudo`. For a replica receiving settings, either explicitly grant that permission or disable all configuration-writing toggles for that replica: `sync_settings`, `sync_local_dns`, `sync_local_cnames`, and `sync_static_dhcp`. List and domain management are separate from configuration writes. Review the permission before enabling it. [Pi-hole application-password permissions](https://docs.pi-hole.net/ftldns/configfile/#app_sudo)

## Quick start with Docker Compose

The public quick start **builds the image from this repository**. It does not assume that a prebuilt public Docker Hub image exists. Maintainer-specific local-registry instructions are in [the deployment guide](docs/DEPLOYMENT.md).

The shell examples below use Bash, including WSL on Windows. Run them from the cloned repository.

```bash
git clone https://github.com/EldieTurner/pihole-sync.git
cd pihole-sync
mkdir -p data
cp config.example.yaml config.yaml
cp .env.example .env
```

Edit `config.yaml` with your Pi-hole API URLs and edit `.env` with the corresponding passwords **before starting the service**. The example addresses are placeholders. Back up each Pi-hole through its own backup/Teleporter tools before the first sync.

The repository's [docker-compose.yml](docker-compose.yml) contains:

```yaml
services:
  pihole-sync:
    build:
      context: .
    image: pihole-sync:local
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

Build, validate the configuration without contacting Pi-hole, then start:

```bash
docker compose build --pull
docker compose run --rm --no-deps pihole-sync python -m pihole_sync --validate
docker compose up -d
docker compose logs -f --tail=100 pihole-sync
```

Open **http://localhost:8080** on the Docker host. The first sync begins shortly after startup. Matching items on replicas can be updated during this first cycle even with `reconcile: false`; this is not a dry run.

The port is bound to loopback because the dashboard can trigger changes and has no authentication. To reach a remote host's dashboard, use a trusted authenticated reverse proxy or an SSH tunnel:

```bash
ssh -L 8080:127.0.0.1:8080 user@docker-host
```

Open http://localhost:8080 on the machine running the tunnel. Use different local ports if port 8080 is already occupied.

## Example config.yaml

The following is also provided in [config.example.yaml](config.example.yaml). Set API base URLs, without `/admin` or `/api` appended.

```yaml
sync:
  interval_seconds: 300
  run_gravity: auto
  restart_dns: auto

  sync_settings: true
  sync_local_dns: true
  sync_local_cnames: true
  sync_static_dhcp: false

  # Scheduled cycles preserve edits made only on a replica.
  reconcile: false

  # Deletion requires explicit opt-in for each object type.
  delete_extra_lists: false
  delete_extra_domains: false
  delete_extra_groups: false
  delete_extra_clients: false

  exclude_config_paths:
    - dhcp
    - webserver
    - files
    # Preserve per-instance DNS settings, including Docker-managed values.
    - dns.port
    - dns.upstreams
    - dns.listeningMode
    - dns.dnssec

primary:
  host: http://192.168.1.20:80
  password: ${PIHOLE_PRIMARY_PASSWORD}
  verify_tls: true

replicas:
  - name: pihole2
    host: http://192.168.1.21:80
    password: ${PIHOLE_REPLICA_PASSWORD}
    verify_tls: true
```

Use unique, stable replica names: the application stores its last-applied snapshots under those names. Add another replica by adding another list entry and a corresponding password variable in `.env`.

Configuration values support `${VARIABLE_NAME}` references. They are expanded by **pihole-sync inside the container**, including references in the mounted YAML file. Referenced variables must exist in the container environment; missing variables cause startup/validation to fail. Shell syntax such as `${NAME:-fallback}` is not supported by this loader.

Providing `exclude_config_paths` **replaces** the default list; it does not append to it. Retain `dhcp`, `webserver`, and `files` when adding your own exclusions. This example also preserves per-instance DNS settings; remove individual DNS exclusions only if you intend to synchronize writable settings across hosts.

For a replica that should receive lists and rules but no configuration settings:

```yaml
replicas:
  - name: lists-only
    host: http://192.168.1.22:80
    password: ${PIHOLE_LISTS_ONLY_PASSWORD}
    sync_settings: false
    sync_local_dns: false
    sync_local_cnames: false
    sync_static_dhcp: false
```

This is an alternative replica entry, not a second `replicas` key to append to the same YAML file.

## Example .env

Copy [.env.example](.env.example) to `.env`, then replace the placeholders:

```dotenv
PIHOLE_PRIMARY_PASSWORD=replace-with-primary-api-password
PIHOLE_REPLICA_PASSWORD=replace-with-replica-api-password
LOG_LEVEL=INFO
```

Use one literal `KEY=value` per line, without shell `export`, surrounding quotes, or inline comments. This convention works with both `docker run --env-file` and the example Compose service's **raw** environment-file format. Characters such as `$` are passed literally. [Docker environment-file documentation](https://docs.docker.com/reference/compose-file/services/#env_file)

Compose's automatic use of `.env` for Compose-file substitution does not, by itself, pass every variable into a container. The explicit `env_file` above does that. Do not publish the output of commands that expand credentials, such as a fully resolved `docker compose config`.

The real `.env` and `config.yaml` are ignored by Git. On Linux, restrict their permissions:

```bash
chmod 600 .env config.yaml
```

On Windows, restrict access through file permissions. Environment variables are not encrypted secret storage: administrators and users with Docker access can inspect them.

## Run with docker run

Use the same `config.yaml`, `.env`, and `data/` directory as above. Build a local image for the current machine's architecture:

```bash
docker build -t pihole-sync:local .

docker run -d \
  --name pihole-sync \
  --restart unless-stopped \
  --env-file .env \
  --publish 127.0.0.1:8080:8080 \
  --mount type=bind,source="$(pwd)/config.yaml",target=/config/config.yaml,readonly \
  --mount type=bind,source="$(pwd)/data",target=/data \
  pihole-sync:local

docker logs -f --tail=100 pihole-sync
```

Choose either Compose or `docker run` for a deployment. Do not start both against the same primary/replicas or share the same state directory between concurrent instances.

The image uses a `CMD`, not an `ENTRYPOINT`. When overriding its command, include `python -m pihole_sync`; appending `--once` or `--validate` directly after the image name is not sufficient.

## Validation and one-shot sync

### Validate without changing any Pi-hole

```bash
docker run --rm \
  --env-file .env \
  --mount type=bind,source="$(pwd)/config.yaml",target=/config/config.yaml,readonly \
  pihole-sync:local python -m pihole_sync --validate
```

Validation loads the configuration and checks environment references and recognized sync option names. It prints a summary without printing passwords. It does **not** test API connectivity, credentials, permissions, or every possible configuration value. There is no preview/dry-run mode for proposed remote changes.

### Run one sync cycle

Stop the background service first, then run a disposable container with the same state:

```bash
docker compose stop pihole-sync
docker compose run --rm --no-deps pihole-sync python -m pihole_sync --once
docker compose up -d
```

For an equivalent `docker run` cycle, stop any existing sync container and run:

```bash
docker run --rm \
  --env-file .env \
  --mount type=bind,source="$(pwd)/config.yaml",target=/config/config.yaml,readonly \
  --mount type=bind,source="$(pwd)/data",target=/data \
  pihole-sync:local python -m pihole_sync --once
```

`--once` performs a **forced reconciliation**, even when `reconcile: false`. The dashboard's **Sync now** button and `POST /api/sync` do the same. They can overwrite replica-only edits and, when deletion options are enabled, remove extra replica items. Deletion remains disabled unless explicitly configured.

Exit codes are `0` for a successful validation/cycle, `1` for a failed one-shot cycle reported by the engine, and `2` for a handled configuration error. An unhandled failure may also produce a nonzero exit code.

## Configuration reference

### Sync options

| Option under `sync` | Default | Behavior |
| --- | --- | --- |
| `interval_seconds` | `300` | Scheduled poll interval; the scheduler uses a minimum of five seconds. Long cycles do not overlap. |
| `run_gravity` | `auto` | `auto`, `always`, or `never`. Auto rebuilds replica gravity after changes to groups, lists, domains, or clients. |
| `restart_dns` | `auto` | `auto`, `always`, or `never`. Auto restarts replica FTL after configuration changes or a gravity action. |
| `sync_settings` | `true` | Synchronize remaining general config leaves after exclusions and dedicated toggles. |
| `sync_local_dns` | `true` | Synchronize `dns.hosts`. |
| `sync_local_cnames` | `true` | Synchronize `dns.cnameRecords`. |
| `sync_static_dhcp` | `false` | Synchronize only `dhcp.hosts`; DHCP server/scope settings remain excluded. |
| `reconcile` | `false` | Repair replica-only drift during scheduled cycles. Forced cycles always reconcile. |
| `delete_extra_lists` | `false` | Allow removal of extra replica list subscriptions. |
| `delete_extra_domains` | `false` | Allow removal of extra replica domain rules. |
| `delete_extra_groups` | `false` | Allow removal of extra replica groups, except the Default group. |
| `delete_extra_clients` | `false` | Allow removal of extra replica managed clients. |
| `exclude_config_paths` | `[dhcp, webserver, files]` | Exclude dotted paths and all descendants by prefix. A supplied list replaces these defaults. |

Per-node sync options can be placed directly inside `primary` or a replica entry. For example, a replica may use `sync_settings: false` or a different `exclude_config_paths` list. The scheduler's poll interval remains global. Keep the primary and replica exclusion lists consistent when excluding shared settings.

Advanced timeout fields currently include `request_timeout` (default `30`) and `gravity_timeout` (default `900`). In 0.2.1, `gravity_timeout` is not wired into the action call: gravity uses a fixed 900-second timeout and DNS restart uses 180 seconds. Ordinary requests currently pass no explicit deadline through the request wrapper, overriding the HTTP client's configured default. Do not rely on `request_timeout` as a hard bound on a sync cycle.

### Pi-hole connection options

| Field | Meaning |
| --- | --- |
| `host` | Required API base URL, such as `http://192.168.1.20:80` or `https://pihole.example.net`. |
| `name` | Friendly name; use a unique, stable name for each replica. Defaults to the host when omitted. |
| `password` | API/application password, usually provided through `${ENV_VAR}`. |
| `totp` | Optional base32 TOTP secret, provided through `${ENV_VAR}`. An application password avoids storing a TOTP seed. |
| `verify_tls` | Defaults to `true`. Applies to HTTPS certificate validation; it does not encrypt HTTP URLs. |

### Application environment and CLI

| Environment variable | CLI override | Default |
| --- | --- | --- |
| `PIHOLE_SYNC_CONFIG` | `--config` | `/config/config.yaml` |
| `PIHOLE_SYNC_STATE` | `--state` | `/data/state.db` |
| `PIHOLE_SYNC_HOST` | `--host` | `0.0.0.0` |
| `PIHOLE_SYNC_PORT` | `--port` | `8080` |
| `LOG_LEVEL` | `--log-level` | `INFO` |

To change the externally published port, change `127.0.0.1:8080:8080` to, for example, `127.0.0.1:8900:8080`. The image healthcheck reads `PIHOLE_SYNC_PORT` if you instead change the internal application port. A CLI-only `--port` override does not update that healthcheck's environment.

## Networking and Docker-managed Pi-hole settings

- **Bridge networking:** use each host's reachable LAN address and published web port. `127.0.0.1` inside the sync container refers to that container.
- **Shared Docker network:** attach the sync container to Pi-hole's user-defined network and use a resolvable service name such as `http://pihole:80`.
- **Linux host networking:** `network_mode: host` allows access to the host's loopback services. Remove `ports` mappings in host mode and explicitly restrict the dashboard's listening address. The public quick start does not require host networking.
- **Remote hosts:** allow the sync container to reach each Pi-hole's HTTP(S) API. The service does not need port 53, SSH, privileged mode, or a Docker socket mount.

Pi-hole settings supplied through `FTLCONF_*` environment variables are read-only through its API. Exclude those paths from sync. The example excludes common DNS settings so each Pi-hole can keep its own upstream resolver and Docker listening configuration. [Pi-hole Docker configuration](https://docs.pi-hole.net/docker/configuration/)

For Pi-hole and Unbound in separate containers, `127.0.0.1` is generally not the Unbound container. Configure and test each Pi-hole's upstream independently. Setting `dns.listeningMode` for a Pi-hole behind a Docker bridge is also separate from this service's dashboard bind address.

## Sync behavior and limitations

Each cycle compares the primary's desired configuration, the replica's current configuration, and the snapshot last successfully applied to that replica. It writes an item when it differs and either changed on the primary or reconciliation is enabled. Group membership IDs are translated by group name.

- The first cycle treats selected primary values as new and can update existing replica objects.
- Scheduled cycles normally preserve replica-only edits when the primary has not changed. Enable `reconcile` to repair that drift.
- With deletion enabled but reconciliation off, removal generally applies to items known from the last successful primary snapshot. Reconciliation also considers other extras on the replica.
- The built-in Default group is retained; its metadata is not updated by the engine.
- Failed replica cycles are not marked as successfully applied and are retried. Remote writes already completed before a failure are **not rolled back**; a sync cycle is not a transaction across Pi-holes.
- Exclusions apply to configuration paths, not to subscribed lists, domain rules, groups, or clients.
- There is no continuous read-only observation mode. Disabling `sync_settings` alone still permits list/rule/client/group changes and separately enabled local DNS writes.
- Configuration is loaded at startup. Recreate/restart the service after editing YAML or changing credentials.

See [ARCHITECTURE.md](docs/ARCHITECTURE.md) for snapshot, diff, and API details.

## Monitoring and troubleshooting

```bash
docker compose ps
docker compose logs --tail=100 pihole-sync
curl --fail http://127.0.0.1:8080/api/status
```

The Docker healthcheck verifies that the dashboard/status endpoint answers HTTP requests. **Healthy does not prove the last sync succeeded.** Inspect the dashboard's primary/replica results, `last_cycle`, and activity history. Previously stored applied snapshots do not prove a replica is currently reachable.

`POST /api/sync` schedules a forced cycle; the HTTP response acknowledges the trigger, not completion. The dashboard reports the eventual result. A no-op cycle should report `no changes` without restarting DNS when the automatic modes are used.

| Symptom | Check |
| --- | --- |
| `unreachable`, refused connection, or timeout | API URL, published web port, Docker routing, firewall, and name resolution from the container. Remove `/admin` or `/api` from the base URL. |
| HTTP 401 | Password, application password validity, or TOTP/clock settings. Generate a new application password if the old one was revoked. |
| HTTP 403 when writing settings | Replica API permissions, including `webserver.api.app_sudo` for application-password sessions. |
| HTTP 429 / API seats exceeded | Other applications or orphaned sessions using Pi-hole's session slots. Stop repeated login attempts, allow sessions to expire, and inspect Pi-hole's API session settings. |
| Repeated identical setting patches or DNS restarts | Align Pi-hole versions, preserve default exclusions, and exclude read-only Docker-managed or host-specific settings. |
| HTTP 400 asking for a list type | Use pihole-sync 0.2.1 or newer; 0.2.1 adds the required query parameter for FTL 6.8 list creation. |
| Missing environment variable | Check `env_file` / `--env-file`; a host shell variable is not automatically passed to Docker. |
| YAML option rejected | Compare spelling and placement with the configuration reference; per-replica options belong directly in that replica entry. |
| SQLite permission or locking error | Ensure `/data` is writable and only one sync process uses the state directory. |
| Certificate verification error | Use a trusted server certificate or configure an appropriate CA trust chain. Disabling `verify_tls` removes server identity verification. |
| Wrong CPU architecture / missing manifest | Build locally for that host or publish an image containing the required architecture. |
| UI unavailable but container is running | Check port conflicts, loopback-only publishing, SSH tunnel, and internal/external port mapping. |

If filtering differs between clients, query each Pi-hole directly and inspect its query log. Browser Secure DNS, VPN DNS, public fallback resolvers, client groups, or cached answers can bypass or obscure otherwise synchronized filtering. This service does not enforce clients' DNS paths.

## Security

- Keep the dashboard and its API private. They have **no built-in authentication**, including the endpoint that triggers forced sync.
- Prefer HTTPS for Pi-hole API connections across untrusted networks. HTTP sends credentials and configuration without transport encryption. Keep `verify_tls: true` when using HTTPS.
- Store credentials in the private `.env`; never commit real passwords or TOTP seeds. The application does not currently implement Docker-secret `_FILE` environment conventions.
- State snapshots can contain internal hostnames, addresses, rules, and settings. Protect backups and `/data`, even though masked secrets are pruned.
- Avoid exposing resolved environment/configuration output in bug reports. Application errors can contain URLs and configuration context; redact logs before sharing them.
- The service does not require host directories containing Pi-hole data, a Docker socket, host administrator credentials, or privileged Docker access.

## Updates, backups, and recovery

For a source-built Compose deployment:

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d --no-deps pihole-sync
docker compose logs --tail=100 pihole-sync
```

Review configuration and release changes first. For a registry deployment, replace the `build` section and local image name with a verified registry image/tag or digest, then use `docker compose pull pihole-sync` before recreating the service. Prefer a fixed release tag or digest when you need reproducible rollbacks.

Back up each Pi-hole separately before significant changes. The sync service's state is not a substitute for Pi-hole backups. To make a consistent file backup of the service, stop it and copy `config.yaml`, `.env`, and the **whole** `data/` directory, then start it again. Stopping this service does not stop Pi-hole DNS.

Restore files with appropriate permissions before restarting. If intentionally discarding state, stop the service and move the entire state directory aside, including SQLite WAL/SHM files. Starting with empty state treats selected primary configuration as new and can reapply it to replicas; review deletion/reconciliation options before doing this.

`docker compose down` stops/removes the sync container and its Compose network. The example uses bind-mounted configuration and data directories, which remain on disk. Deleting those directories is a separate action.

## Development and publishing

Use Python 3.10+; the image uses Python 3.12. From a virtual environment:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m pihole_sync.selftest
python -m compileall -q pihole_sync
```

On Windows, activate with `.venv\Scripts\Activate.ps1`. A direct Python run does not load `.env` automatically: export the referenced variables before running `python -m pihole_sync --config config.yaml --validate`.

The offline tests cover synchronization behavior and HTTP request compatibility without contacting real Pi-holes. Live API tests require reachable Pi-hole instances and appropriate credentials.

To publish a multi-architecture image to **your own** registry, replace `registry.example.com/your-user` with a registry path you control:

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  --tag registry.example.com/your-user/pihole-sync:0.2.1 \
  --provenance=false \
  --push .
```

Use a builder that supports these platforms. A normal local `docker build` creates an image for the current platform; do not overwrite an existing multi-platform registry tag with a single-platform push. Private HTTP registries need explicit Docker trust configuration; prefer TLS for shared/public registries. [Detailed deployment and registry guide](docs/DEPLOYMENT.md)

Contributions are welcome through [issues](https://github.com/EldieTurner/pihole-sync/issues) and pull requests. Include the pihole-sync and Pi-hole versions, deployment method, sanitized configuration, and relevant errors when reporting a bug. Read [AGENTS.md](AGENTS.md) and [ARCHITECTURE.md](docs/ARCHITECTURE.md) before changing the engine or API client.

## License

[MIT](LICENSE). This is an independent community project and is not an official Pi-hole component.
