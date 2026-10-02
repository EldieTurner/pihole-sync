# AGENTS.md — guidance for AI agents (and humans) maintaining this project

This file is written for any AI agent or developer picking up this codebase
later. It tells you how to work safely here, what is already known, and where
the traps are.

## Project summary

pihole-sync keeps multiple **Pi-hole v6** instances identical: a container on
(or next to) the *primary* pihole polls its state and pushes diffs to every
*replica* over the **official pihole v6 REST API**. There is deliberately no
SSH/file-copying. Direction is strictly **primary → replica** (no
bidirectional merge). GitHub project: EldieTurner/pihole-sync.

**Read `docs/ARCHITECTURE.md` first** — it documents the sync-cycle model,
the exact API semantics the code depends on (verified against FTL v6.2.3
sources), and state handling. Do not re-derive these from scratch; extend
them if they change.

## Rules of engagement

1. **Run the offline tests before and after every change:**
   `python -m pihole_sync.selftest`
   This runs the real `SyncEngine` against in-memory pihole fakes. It is the
   regression net — extend `selftest.py` when you change engine logic.
   Also: `python -m compileall -q pihole_sync` for a fast syntax gate.
2. **Never weaken safety defaults.** These are intentional:
   - deletion of extra items is opt-in per kind (`delete_extra_*` default false)
   - `dhcp` (scope settings), `webserver`, `files` subtrees excluded by default
   - masked values (`"********"`) are never read/written — writing a masked
     value back would corrupt a pihole config
   - applied-snapshot is only updated on full replica success (else retries)
   If you must change one, call it out explicitly to the user.
3. **Verify pihole API changes against sources, not memory.** Endpoint specs
   live in the pi-hole/FTL repo at
   `src/api/docs/content/specs/*.yaml` (tagged per version, e.g. `v6.2.3`);
   the web UI's actual calls (`pi-hole/web` repo, `scripts/js/*.js`) are the
   ground truth for tricky call shapes (e.g. `:batchDelete` endpoints,
   URL-encoding of path items). Known subtleties are in
   `docs/ARCHITECTURE.md` → "API semantics".
4. **Respect the engine's diff model.** Three snapshots per replica
   (desired/applied/current); push only
   `drift AND (reconcile OR changed_on_primary)`. Don't turn this into
   "always push everything" without a config flag.
5. **Keep the API client thin and mirror the web UI.** Mutations go through
   `pihole_sync/api.py` only. New pihole features (e.g. new object types)
   get: a getter + CRUD methods in `api.py`, a `norm_*` + snapshot entry +
   keyed sync in `sync.py`, a fake in `selftest.py`, a toggle in `config.py`
   `SyncOptions`, and a dashboard chip.
6. **Backwards compatibility of state is cheap:** `applied:{name}` snapshots
   are versioned implicitly; if you change the snapshot schema, old state
   files will diff weirdly. Bump the stored-shape with a simple version key
   in the state JSON or document that users should delete
   `/data/state.db` after upgrading (it forces a clean initial sync).
7. **Versioning:** bump `pihole_sync/__init__.py` `__version__` for every
   pushed image; build with the multi-arch buildx command (never plain
   `docker build` + push — it would replace the manifest list with an
   amd64-only image; see `docs/DEPLOYMENT.md`).
8. **Secrets:** never commit `deploy/.env`, `config.yaml` (real), or any
   password. `${ENV_VAR}` interpolation is the supported pattern.

## Environment notes

- Development happened on Windows with Python 3.14; the Docker image uses
  python:3.12-slim. Both must stay compatible (no 3.13+-only syntax).
- Dependencies are intentionally minimal: httpx, fastapi, uvicorn, PyYAML.
  Ask before adding more.
- Docker: user's local registry `10.0.0.40:5000` (HTTP, needs
  insecure-registries on every docker host that touches it). Primary pihole
  Pi at 10.0.0.5, replica at 10.0.0.6. Dashboard on the Pi: port 8900
  (`PIHOLE_SYNC_PORT`), because 8080 is taken there.
- Live testing requires the user's LAN (piholes aren't reachable from CI or
  from outside). Validate live changes via `--once` + `docker logs`, or the
  dashboard. The 401/429 paths and multi-arch pull have been verified live;
  the self-test covers the rest.

## Known history (don't re-introduce these bugs)

- FTL **login response** lifetime field is `validity` (relative seconds),
  not `valid_until` — misreading this caused a login-per-request storm and
  `429: API seats exceeded` (sessions are capped AND persisted in
  pihole-FTL.db across restarts; the client now logs out on close).
- 0.1.0 synced per-instance `webserver.*` settings to replicas — that's why
  `webserver` is excluded by default now.
- `127.0.0.1` inside a bridge-mode container is the container itself, not
  the host pihole — deployment docs cover host-network vs LAN-IP.

## Release checklist

1. `python -m pihole_sync.selftest` passes
2. bump `__version__`
3. multi-arch buildx push to the registry (`docs/DEPLOYMENT.md`)
4. `git commit` + `git tag vX.Y.Z` + push (CI publishes to Docker Hub once
   `DOCKERHUB_USERNAME` variable and `DOCKERHUB_TOKEN` secret exist)
5. on the Pi: `docker compose pull && docker compose up -d`, check
   `docker logs -f pihole-sync` for a clean cycle and the dashboard going
   green