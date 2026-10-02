# pihole-sync

Keep multiple [Pi-hole v6](https://pi-hole.net) instances in sync — automatically and with a small web dashboard.

One container runs on (or next to) your **primary** Pi-hole. It polls the primary's configuration and pushes any changes — adlists, allow/deny domains, groups, local DNS records, CNAMEs and settings — to every replica over the **Pi-hole v6 REST API**. No SSH, no key management, no file copying.

```
            ┌──────────────────────┐
            │  primary Pi-hole v6  │◄────┐  poll + read
            │  (source of truth)   │     │
            └──────────────────────┘     │
                                     ┌───┴───────────┐
                                     │  pihole-sync  │── dashboard :8080
                                     └───┬───────┬───┘
                          push diffs ┌───┘       └───┐ push diffs
                             ┌───────▼─────┐   ┌─────▼───────┐
                             │  replica 2  │   │  replica 3  │  (+N more)
                             └─────────────┘   └─────────────┘
```

## Features

- **What gets synced** (configurable):
  - Adlists (`GET/POST/PUT/DELETE /api/lists`) — both `block` and `allow` lists
  - Allowlist / denylist domains, exact & regex (`/api/domains`)
  - Groups — synced by **name**, group IDs are remapped per replica automatically
  - Local DNS records (`dns.hosts`) and Local CNAMEs (`dns.cnameRecords`)
  - General settings via `/api/config` (upstreams, cache, blocking mode, ...)
- **Change detection**: only pushes what actually changed since each replica's last successful sync. Replicas that were offline get caught up automatically.
- **Drift repair** (opt-in `reconcile`): fixes changes made directly on replicas.
- **Safe defaults**: deletion of extra items is opt-in per kind; DHCP settings are excluded by default; masked secrets (`"********"`) are never read or written.
- **Automation hooks**: runs `pihole -g` on replicas after adlist changes and restarts pihole-FTL after settings changes (both configurable: `auto` / `always` / `never`).
- **Dashboard**: replica status, last-sync details, error banners, activity log, and a manual **Sync now** button.
- **Multi-arch Docker image** (amd64, arm64, armv7) — runs on Raspberry Pis.

## Quick start

1. Get a password for API access on each Pi-hole. Recommended: create an **application password** on each instance (*Settings → API / All Settings → `webserver.api.app_pwhash` via the web UI's password generator, or simply use the web interface password*). Application passwords keep working even if you enable 2FA.

2. Create `config.yaml` next to your `docker-compose.yml` (see [config.example.yaml](config.example.yaml)):

   ```yaml
   primary:
     host: http://pihole:80            # container name if they share a network
     password: ${PIHOLE_PRIMARY_PASSWORD}
   replicas:
     - name: pihole2
       host: http://192.168.1.23:80
       password: ${PIHOLE_REPLICA_PASSWORD}
   ```

3. Run it:

   ```bash
   docker compose up -d
   ```

4. Open the dashboard at `http://<docker-host>:8080`. The first sync pushes the full primary state to every replica.

### Validating your config

```bash
docker run --rm -v ./config.yaml:/config/config.yaml:ro \
  -e PIHOLE_PRIMARY_PASSWORD=... yourdockerhubuser/pihole-sync:latest --validate
```

### One-shot sync (cron / systemd timer alternative)

```bash
docker run --rm ... yourdockerhubuser/pihole-sync:latest --once
```

## Configuration reference

| Option | Default | Meaning |
|---|---|---|
| `sync.interval_seconds` | `300` | Poll interval. |
| `sync.run_gravity` | `auto` | `auto` = run `pihole -g` on replicas after adlist/group/domain changes; `always`; `never`. |
| `sync.restart_dns` | `auto` | Restart pihole-FTL after settings changes; `always`; `never`. |
| `sync.sync_settings` | `true` | Sync general settings via `/api/config`. |
| `sync.sync_local_dns` | `true` | Sync local DNS records (`dns.hosts`). |
| `sync.sync_local_cnames` | `true` | Sync CNAME records (`dns.cnameRecords`). |
| `sync.reconcile` | `false` | Also push when the primary hasn't changed, to repair replica drift. |
| `sync.delete_extra_lists` | `false` | Delete adlists on replicas that are gone from the primary. |
| `sync.delete_extra_domains` | `false` | Delete extra allow/deny domains. |
| `sync.delete_extra_groups` | `false` | Delete extra groups. |
| `sync.exclude_config_paths` | `[dhcp, webserver.api.*secrets]` | Config subtrees never synced (prefix match, dot notation). |
| `primary` / `replicas[].host` | — | Base URL of the instance (`http://host[:port]`). |
| `password` / `totp` | — | API password (or application password) / TOTP secret. `${ENV_VAR}` interpolation supported. |
| `verify_tls` | `true` | Verify TLS certificates (set `false` for self-signed HTTPS). |

Every `sync.*` option can be overridden per replica by adding the key inside that replica's block.

Environment variables: `PIHOLE_SYNC_CONFIG`, `PIHOLE_SYNC_STATE`, `PIHOLE_SYNC_HOST`, `PIHOLE_SYNC_PORT`, `LOG_LEVEL`.

## Networking notes

- **Primary in Docker on the same host**: either join the pihole network (see `docker-compose.yml` comments) and use `http://pihole:80`, or simply use the host LAN IP / published port (`http://192.168.1.2:80`).
- **Pi-hole core (bare metal) primary**: `http://localhost:80` with `network_mode: host`, or the host IP.
- The syncer only needs *outbound* HTTP(S) access to each Pi-hole; replicas do not need anything inbound from each other.

## How it decides what to push

For every replica the engine compares three snapshots:

1. **desired** — current state of the primary,
2. **applied** — what the replica was last successfully synced to,
3. **current** — what the replica actually has right now.

An item is pushed when it changed on the primary *and/or* drifted on the replica (the latter only with `reconcile: true`). Group IDs differ between instances, so groups are matched by name and adlist/domain group memberships are remapped per replica. Secrets and excluded paths are stripped from all snapshots before comparison. If a push fails, nothing is marked as applied and the next cycle retries.

## Security notes

- Credentials live in `config.yaml` — keep it `chmod 600`, and prefer `${ENV_VAR}` interpolation or application passwords.
- The dashboard has no authentication; bind it to a trusted network (default `0.0.0.0:8080` — restrict via `PIHOLE_SYNC_HOST` or your reverse proxy).
- The syncer only ever talks to the official Pi-hole v6 API; it does not modify files on the hosts and does not need SSH.

## Development

```bash
pip install -r requirements.txt
python -m pihole_sync.selftest     # offline engine tests (no Pi-hole needed)
python -m pihole_sync --config config.yaml --validate
python -m pihole_sync              # dashboard on :8080
```

The API client in `pihole_sync/api.py` mirrors the official FTL v6 API specs and the v6 web UI's exact call patterns (including `:batchDelete` endpoints).

## FAQ

**Why no SSH?** Pi-hole v6 exposes a complete REST API — lists, domains, groups, config, local DNS and even `pihole -g` remotely. API sync is more robust than SSH file copying (no file corruption, no version skew, works with Docker properly).

**Is the primary authoritative?** Yes. Replicas are mirrors; anything you add directly to a replica will be kept only until you enable `reconcile` (and the relevant `delete_extra_*` options).

**Does it sync DHCP or the web UI password?** No — DHCP is excluded by default (rogue DHCP servers are dangerous) and secrets are always excluded.

**What about Pi-hole v5?** Not supported; v6's API is required.

## License

MIT — see [LICENSE](LICENSE).