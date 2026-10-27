# Deployment guide

## Files in `deploy/`

- `docker-compose.yml` — compose file for the **primary** Pi-hole Pi
- `config.example.yaml` — sanitized syncer config template (primary = 10.0.0.2, replica = 10.0.0.3)
- `.env` — your real passwords (**never committed**; see `.env.example`)

## Primary Pi-hole Pi (docker)

```bash
mkdir -p ~/pihole-sync/data && cd ~/pihole-sync
# copy deploy/docker-compose.yml and deploy/.env.example here
# copy deploy/config.example.yaml as config.yaml and .env.example as .env
# edit .env and review the deployment template before starting
docker compose up -d
docker logs -f pihole-sync
```

Dashboard: `http://<pi-ip>:8900` (set via `PIHOLE_SYNC_PORT`).

The compose uses `network_mode: host` so `http://127.0.0.1:80` reaches the
pihole container's published web port and LAN IPs are directly reachable.
If the pihole web UI is published on another port, change the URL in
`config.yaml`. Without host networking, point `primary.host` at the Pi's LAN
IP instead (`127.0.0.1` inside a bridge container is the container itself).

## Local registry workflow (before publishing to Docker Hub)

Registry assumed at `10.0.0.40:5000` (plain HTTP).

**Build machine (Windows/Docker Desktop):** add the registry to
`%USERPROFILE%\.docker\daemon.json`:

```json
{ "insecure-registries": ["10.0.0.40:5000"] }
```

then restart Docker Desktop. Build and push multi-arch (single-platform
builds will break arm pulls!):

```powershell
docker buildx build --builder desktop-linux `
  --platform linux/amd64,linux/arm64,linux/arm/v7 `
  -t 10.0.0.40:5000/pihole-sync:latest -t 10.0.0.40:5000/pihole-sync:0.2.1 `
  --provenance=false --push .
```

(`--provenance=false` because `registry:2` rejects OCI attestations.)

**Every consumer host (the Pis)** also needs the insecure-registry entry in
`/etc/docker/daemon.json` + `sudo systemctl restart docker`, then:

```bash
docker pull 10.0.0.40:5000/pihole-sync:latest
```

## Updating the syncer on the Pi

```bash
cd ~/pihole-sync
docker compose pull && docker compose up -d
```

## Publishing to Docker Hub (automatic)

The GitHub Actions workflow (`.github/workflows/ci.yml`) runs the offline
self-test on every push. The multi-arch publish job activates when the repo
has:

- **Variable**: `DOCKERHUB_USERNAME`
- **Secret**: `DOCKERHUB_TOKEN`

Then: pushes to `main` → `:latest`, tags `v*` → semver tags. Switch the Pi's
compose `image:` to `docker.io/<user>/pihole-sync:latest` — no
insecure-registry config needed for Docker Hub.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `429: API seats exceeded` | Leaked API sessions (default cap: 16, **persisted across FTL restarts** in `pihole-FTL.db`). They expire ~30 min after last use. Immediate fix: stop pihole, `sqlite3 /etc/pihole/pihole-FTL.db "DELETE FROM session;"`, start pihole. Also raise `webserver.api.max_sessions` (e.g. 64) in the pihole UI. |
| `no matching manifest for linux/arm64/v8` | The pushed image is amd64-only. Use the multi-arch buildx command above. |
| `http: server gave HTTP response to HTTPS client` | Registry not in `insecure-registries` on the machine doing push/pull. |
| Primary `unreachable` in a bridge-mode container | `127.0.0.1` is the container itself; use the host LAN IP or `network_mode: host`. |
| `login failed (401)` | Wrong password, or 2FA enabled → use an application password (Settings → API) instead of the web password. |
| Replica never syncs, primary shows fine | Check replica password and that its web port is reachable from the syncer (`curl http://10.0.0.3/api/auth`). |
| Status stuck at "starting" | Old image with a 60 s healthcheck interval; pull ≥ 0.1.3 (first check ~10 s). |
| Wrong `webserver.paths.prefix` / `serve_all` on a replica | 0.1.0-era bug synced per-instance webserver settings; ≥ 0.1.1 excludes `webserver.*`. Revert manually on the replica (Settings → All Settings). |

## Verifying a sync by hand

```bash
# Stop the background syncer first; --once forces reconciliation.
# On the syncer host — one full forced cycle, then exit:
docker run --rm --env-file .env -v $PWD/config.yaml:/config/config.yaml:ro \
  -v $PWD/data:/data 10.0.0.40:5000/pihole-sync:latest python -m pihole_sync --once

# What the API sees (replace host/password):
docker exec -it pihole-sync python -c "..."   # or use the dashboard /api/status
```

Compare two instances' adlists quickly via each pihole's UI (Tools → ...) or
`GET /api/lists` on both.

## 0.2.1 compatibility and Docker-managed settings

This release fixes list creation against FTL 6.8 by supplying `type` as a
query parameter. It also includes the sanitized deployment template for
the current 10.0.0.2 / 10.0.0.3 pair. That template preserves webserver and
filesystem settings and excludes DNS settings controlled by Docker's
`FTLCONF_dns_upstreams`, `FTLCONF_dns_listeningMode`, and `FTLCONF_dns_dnssec`.
These exclusions belong to this Docker deployment; general installations
can still synchronize DNS settings when they are writable.

Keep the two Pi-hole instances on the same version to avoid repeated
attempts to apply settings an older replica does not support. A no-op sync
cycle should report `no changes` and should not restart FTL.

If upgrading the repaired primary, the old Compose file may contain a
read-only `/app/pihole_sync/api.py` override. After pulling 0.2.1, remove
that one override mount before recreating only `pihole-sync`; the fix is
included in the image. Retain the config and data mounts.
