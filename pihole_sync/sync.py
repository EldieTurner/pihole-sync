"""Snapshot fetching, diffing and replica synchronisation."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .api import AuthError, PiholeClient, PiholeError
from .config import MASKED_VALUE, Node, SyncOptions

log = logging.getLogger("pihole_sync.sync")

# Secrets masked by the API must never be compared or written back.
SECRET_PATHS = (
    "webserver.api.password",
    "webserver.api.pwhash",
    "webserver.api.app_pwhash",
    "webserver.api.totp_secret",
)

DEFAULT_GROUP_NAME = "Default"
_SENTINEL = object()


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
def norm_group(g: dict) -> dict:
    return {
        "id": int(g.get("id") or 0),
        "name": g.get("name") or "",
        "comment": g.get("comment") or "",
        "enabled": bool(g.get("enabled", True)),
    }


def norm_list(l: dict) -> dict:
    return {
        "address": l.get("address") or "",
        "type": l.get("type") or "block",
        "comment": l.get("comment") or "",
        "groups": sorted({int(x) for x in (l.get("groups") or [0])}),
        "enabled": bool(l.get("enabled", True)),
    }


def norm_client(c: dict) -> dict:
    return {
        "client": c.get("client") or "",
        "comment": c.get("comment") or "",
        "groups": sorted({int(x) for x in (c.get("groups") or [0])}),
    }


def norm_domain(d: dict) -> dict:
    return {
        "domain": d.get("domain") or "",
        "type": d.get("type") or "deny",
        "kind": d.get("kind") or "exact",
        "comment": d.get("comment") or "",
        "groups": sorted({int(x) for x in (d.get("groups") or [0])}),
        "enabled": bool(d.get("enabled", True)),
    }


def _prune_config(cfg: Any, exclude_prefixes: tuple, prefix: str = "") -> Any:
    """Return a copy of the config tree without excluded subtrees and secrets."""
    if isinstance(cfg, dict):
        out = {}
        for k, v in cfg.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            # dhcp.hosts (static assignments) is exempt from subtree exclusion;
            # the leaf-toggle filter decides whether it syncs.
            if path != "dhcp.hosts" and (_excluded(path, exclude_prefixes)
                                         or (isinstance(v, str) and v == MASKED_VALUE)):
                continue
            out[k] = _prune_config(v, exclude_prefixes, path)
        return out
    if isinstance(cfg, list):
        return [_prune_config(v, exclude_prefixes, prefix) for v in cfg]
    return cfg


def _excluded(path: str, prefixes: tuple) -> bool:
    return path in prefixes or any(path.startswith(p + ".") for p in prefixes)


def config_leaves(cfg: Any, prefix: str = "") -> dict:
    """Flatten a (pruned) config tree to {dotted.path: leaf_value}."""
    out = {}
    if isinstance(cfg, dict):
        for k, v in cfg.items():
            out.update(config_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = cfg
    return out


def build_patch(leaves: dict) -> dict:
    """Turn {dotted.path: value} into a nested dict for PATCH /api/config."""
    patch: dict = {}
    for path, value in leaves.items():
        node = patch
        parts = path.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return patch


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------
async def make_snapshot(client: PiholeClient, opts: SyncOptions) -> dict:
    """Fetch groups/lists/domains/config from one instance (normalised)."""
    excludes = tuple(opts.exclude_config_paths or ()) + tuple(SECRET_PATHS)
    return {
        "groups": [norm_group(g) for g in await client.get_groups()],
        "lists": [norm_list(l) for l in await client.get_lists()],
        "domains": [norm_domain(d) for d in await client.get_domains()],
        "clients": [norm_client(c) for c in await client.get_clients()],
        "config": _prune_config(await client.get_config(), excludes),
        "version": await client.get_version(),
        "fetched_at": time.time(),
    }


def leaf_toggle_filter(snap_config: dict, opts: SyncOptions) -> dict:
    """Restrict a pruned config snapshot to the leaves enabled by toggles."""
    leaves = config_leaves(snap_config)
    keep = {}
    for path, value in leaves.items():
        if path == "dns.hosts":
            if opts.sync_local_dns:
                keep[path] = value
        elif path == "dns.cnameRecords":
            if opts.sync_local_cnames:
                keep[path] = value
        elif path.startswith("dhcp."):
            # Only static assignments may ever sync (and DHCP only then);
            # all other DHCP leaves are never synced.
            if path == "dhcp.hosts" and opts.sync_static_dhcp:
                keep[path] = value
        elif opts.sync_settings:
            keep[path] = value
    return keep


def snapshot_for_compare(snap: dict, opts: SyncOptions) -> dict:
    """The comparable part of a snapshot (applies leaf toggles)."""
    return {
        "groups": sorted(snap["groups"], key=lambda g: g["name"]),
        "lists": sorted(snap["lists"], key=lambda l: (l["address"], l["type"])),
        "domains": sorted(snap["domains"], key=lambda d: (d["type"], d["kind"], d["domain"])),
        "clients": sorted(snap.get("clients") or [], key=lambda c: c["client"]),
        "config": leaf_toggle_filter(snap["config"], opts),
    }


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
@dataclass
class KindOps:
    added: int = 0
    updated: int = 0
    removed: int = 0

    def any(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def as_dict(self) -> dict:
        return {"added": self.added, "updated": self.updated, "removed": self.removed}


@dataclass
class ReplicaResult:
    name: str
    host: str
    ok: bool = True
    error: str = ""
    groups: KindOps = field(default_factory=KindOps)
    lists: KindOps = field(default_factory=KindOps)
    domains: KindOps = field(default_factory=KindOps)
    clients: KindOps = field(default_factory=KindOps)
    config_leaves: int = 0
    gravity_ran: bool = False
    restart_ran: bool = False
    duration: float = 0.0

    def changes_summary(self) -> str:
        parts = []
        for label, ops in (("groups", self.groups), ("lists", self.lists),
                           ("domains", self.domains), ("clients", self.clients)):
            if ops.any():
                parts.append(f"{label}: +{ops.added}/~{ops.updated}/-{ops.removed}")
        if self.config_leaves:
            parts.append(f"settings: {self.config_leaves} changed")
        if self.gravity_ran:
            parts.append("gravity")
        if self.restart_ran:
            parts.append("restartFTL")
        return ", ".join(parts) if parts else "no changes"


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------
class SyncEngine:
    def __init__(self, config, state, client_factory=PiholeClient):
        self.config = config
        self.state = state
        self.client_factory = client_factory
        self.last_primary_snapshot: Optional[dict] = None
        self.last_cycle: Optional[dict] = None
        self.running = False

    # ------------------------------------------------------------------
    def _client(self, node: Node) -> PiholeClient:
        return self.client_factory(
            host=node.host,
            password=node.password,
            totp=node.totp,
            verify_tls=node.verify_tls,
            timeout=node.options.request_timeout,
        )

    # ------------------------------------------------------------------
    async def run_cycle(self, reason: str = "interval", force: bool = False) -> dict:
        """One full poll: fetch primary snapshot, push diffs to all replicas."""
        if self.running:
            return {"ok": False, "error": "sync already running"}
        self.running = True
        started = time.time()
        try:
            return await self._run_cycle_inner(reason, force)
        finally:
            self.running = False

    async def _run_cycle_inner(self, reason: str, force: bool) -> dict:
        summary: dict = {"ts": time.time(), "reason": reason, "ok": True, "replicas": []}
        started = time.time()
        pri = self.config.primary
        try:
            async with self._client(pri) as client:
                desired = await make_snapshot(client, pri.options)
            self.last_primary_snapshot = desired
        except (PiholeError, AuthError) as e:
            log.error("Primary %s: %s", pri.host, e)
            summary.update(ok=False, primary_ok=False, primary_error=str(e))
            self.state.append_history({"ts": summary["ts"], "reason": reason, "ok": False,
                                       "message": f"primary: {e}"})
            self.last_cycle = summary
            return summary
        summary["version"] = desired.get("version")

        results = []
        for replica in self.config.replicas:
            res = await self._sync_replica(replica, desired, force)
            results.append(res)
            if not res.ok:
                summary["ok"] = False
        summary["replicas"] = results
        summary["duration"] = round(time.time() - started, 2)

        msg = " | ".join(f"{r.name}: {r.changes_summary()}" for r in results) or "no replicas"
        if not summary["ok"]:
            msg += f" (errors: {sum(1 for r in results if not r.ok)})"
        self.state.append_history({"ts": summary["ts"], "reason": reason,
                                   "ok": summary["ok"], "message": msg})
        log.info("Sync cycle (%s) finished in %.1fs: %s", reason, summary["duration"], msg)
        self.last_cycle = summary
        return summary

    # ------------------------------------------------------------------
    async def _sync_replica(self, replica: Node, desired: dict, force: bool) -> ReplicaResult:
        opts = replica.options
        if force:
            opts = opts.override({"reconcile": True}, None)
        res = ReplicaResult(name=replica.name, host=replica.host)
        t0 = time.time()
        applied = self.state.applied_snapshot(replica.name)
        try:
            async with self._client(replica) as client:
                await self._sync_replica_inner(client, replica, desired, applied, opts, res)
        except (PiholeError, AuthError) as e:
            res.ok = False
            res.error = str(e)
            log.error("Replica %s: %s", replica.name, e)
        res.duration = round(time.time() - t0, 2)
        if res.ok:
            # Store what we attempted; failed pushes are retried next cycle.
            self.state.set_applied_snapshot(replica.name, desired)
        else:
            log.warning("Replica %s: not marking as applied (errors present)", replica.name)
        return res

    async def _sync_replica_inner(self, client: PiholeClient, replica: Node,
                                  desired: dict, applied: Optional[dict],
                                  opts: SyncOptions, res: ReplicaResult) -> None:
        compare = snapshot_for_compare(desired, opts)
        applied_cmp = snapshot_for_compare(applied, opts) if applied else None
        current_cmp = snapshot_for_compare(await make_snapshot(client, replica.options), opts)

        # 1) groups: create missing, update drifted (delete opt-in comes last)
        group_ops = await self._sync_groups(client, compare, applied_cmp, current_cmp, opts, res)

        # 2) lists, domains and clients (need group-id map after group creation)
        r_groups = await client.get_groups()
        id_map = self._build_group_id_map(compare["groups"], [norm_group(g) for g in r_groups])
        list_ops = await self._sync_keyed(
            client, "lists", compare["lists"],
            applied_cmp["lists"] if applied_cmp else None, current_cmp["lists"],
            id_map, opts.delete_extra_lists, opts, res)
        domain_ops = await self._sync_keyed(
            client, "domains", compare["domains"],
            applied_cmp["domains"] if applied_cmp else None, current_cmp["domains"],
            id_map, opts.delete_extra_domains, opts, res)
        client_ops = await self._sync_clients(
            client, compare["clients"], applied_cmp["clients"] if applied_cmp else None,
            current_cmp["clients"], id_map, opts, res)

        # 3) extra group deletion (opt-in; after lists/domains no longer reference them)
        if opts.delete_extra_groups:
            await self._delete_extra_groups(client, compare["groups"], current_cmp["groups"],
                                            applied_cmp["groups"] if applied_cmp else None, opts, res)

        # 4) settings / local DNS
        await self._sync_config(client, compare["config"],
                                applied_cmp["config"] if applied_cmp else None,
                                current_cmp["config"], opts, res)

        # 5) gravity / FTL restart
        items_changed = group_ops or list_ops or domain_ops or client_ops
        if opts.run_gravity == "always" or (opts.run_gravity == "auto" and items_changed):
            log.info("Replica %s: starting gravity update...", replica.name)
            await client.run_gravity()
            res.gravity_ran = True
        if opts.restart_dns == "always" or (opts.restart_dns == "auto" and (res.config_leaves or res.gravity_ran)):
            log.info("Replica %s: restarting pihole-FTL...", replica.name)
            await client.restart_dns()
            res.restart_ran = True

    # ------------------------------------------------------------------
    async def _sync_groups(self, client, compare, applied_cmp, current_cmp, opts, res) -> int:
        ops = 0
        p_groups = {g["name"]: g for g in compare["groups"]}
        r_groups = {g["name"]: g for g in current_cmp["groups"]}
        a_groups = {g["name"]: g for g in applied_cmp["groups"]} if applied_cmp else {}
        for gname, g in p_groups.items():
            if gname == DEFAULT_GROUP_NAME:
                continue
            r = r_groups.get(gname)
            p_meta = (g["comment"], g["enabled"])
            if r is None:
                log.info("Replica %s: adding group %r", res.name, gname)
                await client.add_group(gname, g["comment"], g["enabled"])
                res.groups.added += 1
                ops += 1
            else:
                r_meta = (r["comment"], r["enabled"])
                a = a_groups.get(gname)
                changed_since_applied = a is None or (a["comment"], a["enabled"]) != p_meta
                if r_meta != p_meta and (opts.reconcile or changed_since_applied):
                    log.info("Replica %s: updating group %r", res.name, gname)
                    await client.update_group(gname, g["comment"], g["enabled"])
                    res.groups.updated += 1
                    ops += 1
        return ops

    async def _sync_clients(self, client, desired: list, applied: Optional[list],
                            current: list, id_map: dict, opts, res) -> int:
        ops = 0
        p_map = {c["client"]: c for c in desired}
        r_map = {c["client"]: c for c in current}
        a_map = {c["client"]: c for c in (applied or [])}
        for ident, p in p_map.items():
            p_rep = dict(p, groups=[id_map.get(gid, 0) for gid in p["groups"]])
            r = r_map.get(ident)
            changed_since_applied = ident not in a_map or a_map[ident] != p
            drift = r is None or r["comment"] != p["comment"] or set(r["groups"]) != set(p_rep["groups"])
            if not (drift and (opts.reconcile or changed_since_applied)):
                continue
            if r is None:
                log.info("Replica %s: adding client %r", res.name, ident)
                await client.add_client(ident, p["comment"], p_rep["groups"])
                res.clients.added += 1
            else:
                log.info("Replica %s: updating client %r", res.name, ident)
                await client.update_client(ident, p["comment"], p_rep["groups"])
                res.clients.updated += 1
            ops += 1
        if opts.delete_extra_clients:
            for ident in r_map:
                if ident in p_map:
                    continue
                if not (opts.reconcile or ident in a_map):
                    continue
                log.info("Replica %s: removing client %r", res.name, ident)
                await client.delete_clients([ident])
                res.clients.removed += 1
                ops += 1
        return ops

    @staticmethod
    def _build_group_id_map(p_groups: list, r_groups: list) -> dict:
        r_by_name = {g["name"]: g["id"] for g in r_groups}
        return {g["id"]: r_by_name.get(g["name"], 0) for g in p_groups}

    async def _sync_keyed(self, client, kind: str, desired: list, applied: Optional[list],
                          current: list, id_map: dict, delete_extra: bool,
                          opts: SyncOptions, res: ReplicaResult) -> int:
        ops = 0
        if kind == "lists":
            keyf = lambda l: (l["address"], l["type"])
        else:
            keyf = lambda d: (d["type"], d["kind"], d["domain"])
        p_map = {keyf(x): x for x in desired}
        r_map = {keyf(x): x for x in current}
        a_map = {keyf(x): x for x in (applied or [])}

        for key, p in p_map.items():
            # Translate desired item into replica group-id space for drift compare.
            p_rep = dict(p, groups=[id_map.get(gid, 0) for gid in p["groups"]])
            r = r_map.get(key)
            changed_since_applied = key not in a_map or a_map[key] != p
            drift = r is None or self._item_differs(kind, r, p_rep)
            if not (drift and (opts.reconcile or changed_since_applied)):
                continue
            if kind == "lists":
                if r is None:
                    log.info("Replica %s: adding %s list %r", res.name, p["type"], p["address"])
                    await client.add_list(p["address"], p["type"], p["comment"], p_rep["groups"], p["enabled"])
                    res.lists.added += 1
                else:
                    log.info("Replica %s: updating %s list %r", res.name, p["type"], p["address"])
                    await client.update_list(p["address"], p["type"], p["comment"], p_rep["groups"], p["enabled"])
                    res.lists.updated += 1
            else:
                if r is None:
                    log.info("Replica %s: adding %s/%s domain %r", res.name, p["type"], p["kind"], p["domain"])
                    await client.add_domain(p["type"], p["kind"], p["domain"], p["comment"], p_rep["groups"], p["enabled"])
                    res.domains.added += 1
                else:
                    log.info("Replica %s: updating %s/%s domain %r", res.name, p["type"], p["kind"], p["domain"])
                    await client.update_domain(p["type"], p["kind"], p["domain"], p["comment"], p_rep["groups"], p["enabled"])
                    res.domains.updated += 1
            ops += 1

        if delete_extra:
            for key, r in r_map.items():
                if key in p_map:
                    continue
                if not (opts.reconcile or key in a_map):
                    continue
                if kind == "lists":
                    log.info("Replica %s: removing list %r (%s)", res.name, key[0], key[1])
                    await client.delete_lists([{"item": key[0], "type": key[1]}])
                    res.lists.removed += 1
                else:
                    log.info("Replica %s: removing domain %r (%s/%s)", res.name, key[2], key[0], key[1])
                    await client.delete_domains([{"item": key[2], "type": key[0], "kind": key[1]}])
                    res.domains.removed += 1
                ops += 1
        return ops

    @staticmethod
    def _item_differs(kind: str, r: dict, p_rep: dict) -> bool:
        meta = ("comment", "enabled") + (("address", "type") if kind == "lists"
                                         else ("domain", "type", "kind"))
        if any(r[k] != p_rep[k] for k in meta):
            return True
        return set(r["groups"]) != set(p_rep["groups"])

    async def _delete_extra_groups(self, client, desired_groups, current_groups,
                                   applied_groups, opts, res) -> None:
        p_names = {g["name"] for g in desired_groups}
        a_names = {g["name"] for g in (applied_groups or [])}
        extra = [g["name"] for g in current_groups
                 if g["name"] not in p_names and g["name"] != DEFAULT_GROUP_NAME
                 and (opts.reconcile or g["name"] in a_names)]
        if extra:
            log.info("Replica %s: removing groups %s", res.name, extra)
            await client.delete_groups(extra)
            res.groups.removed += len(extra)

    async def _sync_config(self, client, desired_cfg, applied_cfg, current_cfg,
                           opts, res) -> None:
        p_leaves = config_leaves(desired_cfg)
        c_leaves = config_leaves(current_cfg)
        a_leaves = config_leaves(applied_cfg) if applied_cfg is not None else None
        patch_leaves = {}
        for path, value in p_leaves.items():
            if c_leaves.get(path, _SENTINEL) == value:
                continue
            changed_since_applied = a_leaves is None or a_leaves.get(path) != value
            if opts.reconcile or changed_since_applied:
                patch_leaves[path] = value
        if not patch_leaves:
            return
        patch = build_patch(patch_leaves)
        log.info("Replica %s: patching %d setting(s): %s", res.name, len(patch_leaves),
                 ", ".join(sorted(patch_leaves)))
        await client.patch_config(patch)
        res.config_leaves += len(patch_leaves)