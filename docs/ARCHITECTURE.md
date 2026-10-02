# Architecture

This document explains how pihole-sync works internally, the exact API
semantics it relies on, and the gotchas baked into the design. Read this
before changing the sync engine.

## Components

```
main.py          entrypoint: CLI args, config load, wiring, uvicorn + scheduler
config.py        YAML config + ${ENV_VAR} interpolation -> dataclasses
pihole_sync/api.py        PiholeClient (httpx, async) — auth, CRUD, actions
pihole_sync/sync.py       snapshots, diffing, SyncEngine (the core)
pihole_sync/state.py      SQLite state store (per-replica applied snapshots, history)
pihole_sync/scheduler.py  asyncio poll loop + manual-trigger support
pihole_sync/web.py        FastAPI: /api/status, /api/sync, static dashboard
pihole_sync/selftest.py   offline end-to-end tests against an in-memory fake Pi-hole
static/          dashboard (single-file HTML/CSS/JS, no build step)
```

## The sync cycle (`SyncEngine.run_cycle`)

1. **Fetch primary snapshot** (`make_snapshot`): `GET /api/groups`,
   `/api/lists`, `/api/domains`, `/api/clients`, `/api/config`,
   `/api/info/version`. Everything is normalised
   (`norm_group`/`norm_list`/`norm_domain`/`norm_client`) — readonly fields
   (`id`, `date_*`, `number`, `status`, `unicode`, ...) are dropped, comments
   are normalised to `""`.
2. **For each replica**, fetch the same snapshot from the replica.
3. Compare three views (see "Change detection" below) and **push only
   differing items**.
4. **Opt-in deletions**: extra items on the replica are deleted only when the
   corresponding `delete_extra_*` option is set AND the item was known to the
   replica's applied snapshot (or `reconcile` is on).
5. **Gravity / FTL restart**: gravity runs when any list/domain/group/client
   changed (mode `auto`); FTL restarts when settings changed or gravity ran.
6. On full success the replica's **applied snapshot** is stored in SQLite.
   On any failure nothing is marked applied, so the next cycle retries.

## Change detection

Three snapshots are involved per replica:

| name | source |
|---|---|
| **desired** | fresh snapshot of the primary |
| **applied** | snapshot stored in `state.db` from the last successful sync of *this* replica |
| **current** | fresh snapshot of the replica |

For each item (group/adlist/domain/client/setting leaf):

- `changed_on_primary` = `desired != applied` (or applied missing → first run)
- `drift` = `desired != current`

Push when `drift AND (reconcile OR changed_on_primary)`.

Consequences worth remembering:

- A replica that was offline for a while catches up automatically.
- Manual edits on replicas are ignored unless `reconcile: true` (default off).
- If a push fails, `applied` is not updated → automatic retry next cycle.
  Pushes are idempotent, so retries are safe.

## Group-ID remapping

Adlists/domains/clients reference groups by **numeric ID**, and IDs differ
between instances (they depend on creation order; only `Default` is always
`0`). Therefore:

1. Groups are synced first, keyed by **name** (create missing, update
   comment/enabled).
2. The engine re-fetches the replica's groups and builds `primary-id →
   replica-id` by name. Unknown names fall back to group `0` (Default).
3. When pushing a list/domain/client, its `groups` array is translated into
   replica ID space — for drift comparison *and* for the request payload.

The applied snapshot stores items in *primary ID space*; the current replica
snapshot is in *replica ID space*. The drift comparison happens after
translating the desired item (`p_rep` in `_sync_keyed`).

## Config-tree syncing (`/api/config`)

- The config tree is **pruned** before anything else: excluded subtrees
  (prefix match on the dotted path) and **masked values** (FTL returns
  `"********"` for secrets — writing those back would corrupt the config!).
- Default excludes: `dhcp` (except `dhcp.hosts` if `sync_static_dhcp`),
  `webserver` (per-instance serving config), `files` (host file paths), plus
  the secret paths.
- `leaf_toggle_filter` then keeps only leaves allowed by the toggles:
  `dns.hosts` (`sync_local_dns`), `dns.cnameRecords` (`sync_local_cnames`),
  `dhcp.hosts` (`sync_static_dhcp`), everything else (`sync_settings`).
- Diffing is **per leaf** (`config_leaves` flattens the tree to dotted paths).
  The patch is rebuilt nested (`build_patch`) and sent as a single
  `PATCH /api/config` with `{"config": {...}}`.
- `sync_local_dns`/`sync_local_cnames`/`sync_static_dhcp` toggles are applied
  on *both* sides (desired and applied/current views) so turning a toggle on
  or off correctly triggers or suppresses one sync of that leaf.

## API semantics (verified against FTL v6.2.3 sources & specs)

Key facts the client depends on — verify these if Pi-hole changes:

- **Auth**: `POST /api/auth {"password": ...}`. The **login response** puts
  the remaining lifetime in `session.validity` (relative seconds), *not*
  `valid_until` (absolute epoch — only in session listings). Sessions are
  sent via the `X-FTL-SID` header; CSRF is only needed for cookie auth.
- **Session seats**: FTL limits concurrent sessions (`webserver.api.max_sessions`,
  default 16) and **persists them in pihole-FTL.db across restarts**.
  The client therefore logs out (`DELETE /api/auth`) on close, and re-auths
  only on 401. If you see `429: API seats exceeded`, leaked sessions exist —
  they expire ~30 min after last use, or purge `session` table while FTL is
  stopped.
- **Create/update/delete**:
  - `POST /api/groups {"name", "comment", "enabled"}`,
    `PUT /api/groups/{name}` (URL-encoded), `POST /api/groups:batchDelete`
    `[{"item": name}]`
  - `POST /api/lists {"address", "type": block|allow, "comment", "groups",
    "enabled"}`, `PUT /api/lists/{address-url-encoded}?type=...`,
    `POST /api/lists:batchDelete [{"item", "type"}]`
  - `POST /api/domains/{allow|deny}/{exact|regex} {"domain", ...}`,
    `PUT /api/domains/{type}/{kind}/{domain-url-encoded}`,
    `POST /api/domains:batchDelete [{"item", "type", "kind"}]`
  - `POST /api/clients {"client", "comment", "groups"}`,
    `PUT /api/clients/{client-url-encoded}`,
    `POST /api/clients:batchDelete [{"item"}]`
  - The `:batchDelete` endpoints are what the official web UI uses; they
    avoid URL-encoding pitfalls in DELETE paths.
- **Path encoding**: items in URL paths must be percent-encoded with
  `quote(x, safe="")` (addresses contain `://`, regexes contain special
  chars). FTL reads the raw URI and does NOT percent-decode path items, but
  its own web UI encodes them — mirror the web UI exactly.
- **Config patch**: `PATCH /api/config` with a nested partial tree, e.g.
  `{"config": {"dns": {"upstreams": [...]}}}`. Arrays are replaced wholesale.
- **Actions**: `POST /api/action/gravity` (runs `pihole -g`, can take
  minutes — use the long gravity timeout), `POST /api/action/restartdns`.
- **Domains**: type is `allow|deny`, kind is `exact|regex`. Lists: type is
  `allow|block`. Clients are identified by the `client` string (IP, MAC,
  name, or `interface:name`).
- **Static DHCP assignments** live in config at `dhcp.hosts` (array of
  strings). DHCP scope settings (`dhcp.active`, `dhcp.start`, ...) are never
  synced.

## State

SQLite (WAL) at `/data/state.db` with a single `state(key, value)` JSON
table. Keys:

- `applied:{replica-name}` — last successfully pushed snapshot (JSON)
- `history` — ring buffer (last 100) of cycle summaries for the dashboard

State is disposable: deleting the DB forces a full initial push on the next
cycle (still non-destructive unless `delete_extra_*` is enabled).

## Dashboard

`web.py` exposes `GET /api/status` (aggregated engine + state view),
`POST /api/sync` (manual trigger; 409 if a cycle is running), and the static
single-page dashboard. No auth — bind to a trusted network.

## Testing

`selftest.py` runs the real `SyncEngine` against `FakePihole`/`FakeClient`
(in-memory pihole models with the same method surface as `PiholeClient`,
injected via `client_factory`). It covers: initial full sync + group remap,
no-op cycles, primary-change propagation, opt-in deletion, drift repair via
reconcile, and client sync. Run with `python -m pihole_sync.selftest`.
Extend this file when changing engine logic — it is the regression net.