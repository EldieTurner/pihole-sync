"""Offline self-test: runs the engine against an in-memory fake Pi-hole API.

Run with: python -m pihole_sync.selftest
"""

from __future__ import annotations

import asyncio
import json
import tempfile

import httpx

from .api import PiholeClient

from .config import SyncOptions
from .config import Node
from .state import StateStore
from .sync import SyncEngine, build_patch, config_leaves


class FakePihole:
    """In-memory model of a Pi-hole v6 instance (groups/lists/domains/config)."""

    def __init__(self, password="pw"):
        self.password = password
        self.groups = [{"id": 0, "name": "Default", "comment": "", "enabled": True}]
        self.lists = []
        self.domains = []
        self.clients = []
        self.config = {
            "dns": {"upstreams": ["8.8.8.8"], "hosts": [], "cnameRecords": [],
                    "cache": {"size": 10000}},
            "webserver": {"api": {"password": "********", "app_pwhash": ""}},
            "misc": {"nice": 0},
        }
        self.next_group_id = 1
        self.gravity_calls = 0
        self.restart_calls = 0

    def dump(self):
        return {
            "groups": [dict(g) for g in self.groups],
            "lists": [dict(l) for l in self.lists],
            "domains": [dict(d) for d in self.domains],
            "clients": [dict(c) for c in self.clients],
            "config": json.loads(json.dumps(self.config)),
        }


class FakeClient:
    """Implements the same surface as PiholeClient against a FakePihole."""

    def __init__(self, fake: FakePihole, host: str, **kw):
        self.fake = fake
        self.host = host

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def get_groups(self):
        return [dict(g) for g in self.fake.groups]

    async def get_lists(self):
        return [dict(l) for l in self.fake.lists]

    async def get_domains(self):
        return [dict(d) for d in self.fake.domains]

    async def get_clients(self):
        return [dict(c) for c in self.fake.clients]

    async def get_config(self):
        return json.loads(json.dumps(self.fake.config))

    async def get_version(self):
        return {"core": {"local": {"version": "v6.1.4"}}}

    async def add_group(self, name, comment="", enabled=True):
        if any(g["name"] == name for g in self.fake.groups):
            raise AssertionError(f"group {name} exists")
        gid = self.fake.next_group_id
        self.fake.next_group_id += 1
        self.fake.groups.append({"id": gid, "name": name, "comment": comment, "enabled": enabled})
        return {"groups": [{"id": gid}]}

    async def update_group(self, name, comment="", enabled=True):
        for g in self.fake.groups:
            if g["name"] == name:
                g.update(comment=comment, enabled=enabled)
                return
        raise AssertionError(f"group {name} missing")

    async def delete_groups(self, names):
        self.fake.groups = [g for g in self.fake.groups if g["name"] not in names]

    async def add_list(self, address, ltype, comment="", groups=None, enabled=True):
        self.fake.lists.append({"address": address, "type": ltype, "comment": comment,
                                "groups": list(groups or [0]), "enabled": enabled})

    async def update_list(self, address, ltype, comment="", groups=None, enabled=True):
        for l in self.fake.lists:
            if l["address"] == address and l["type"] == ltype:
                l.update(comment=comment, groups=list(groups or [0]), enabled=enabled)
                return
        raise AssertionError(f"list {address} missing")

    async def delete_lists(self, items):
        for it in items:
            self.fake.lists = [l for l in self.fake.lists
                               if not (l["address"] == it["item"] and l["type"] == it["type"])]

    async def add_domain(self, dtype, kind, domain, comment="", groups=None, enabled=True):
        self.fake.domains.append({"domain": domain, "type": dtype, "kind": kind,
                                  "comment": comment, "groups": list(groups or [0]),
                                  "enabled": enabled})

    async def update_domain(self, dtype, kind, domain, comment="", groups=None, enabled=True):
        for d in self.fake.domains:
            if (d["domain"], d["type"], d["kind"]) == (domain, dtype, kind):
                d.update(comment=comment, groups=list(groups or [0]), enabled=enabled)
                return
        raise AssertionError(f"domain {domain} missing")

    async def delete_domains(self, items):
        for it in items:
            self.fake.domains = [d for d in self.fake.domains
                                 if not (d["domain"] == it["item"] and d["type"] == it["type"]
                                         and d["kind"] == it["kind"])]

    async def add_client(self, client, comment="", groups=None):
        if any(c["client"] == client for c in self.fake.clients):
            raise AssertionError(f"client {client} exists")
        self.fake.clients.append({"client": client, "comment": comment,
                                  "groups": list(groups or [0])})

    async def update_client(self, client, comment="", groups=None):
        for c in self.fake.clients:
            if c["client"] == client:
                c.update(comment=comment, groups=list(groups or [0]))
                return
        raise AssertionError(f"client {client} missing")

    async def delete_clients(self, clients):
        self.fake.clients = [c for c in self.fake.clients if c["client"] not in clients]

    async def patch_config(self, patch):
        def apply(node, p):
            for k, v in p.items():
                if isinstance(v, dict):
                    apply(node.setdefault(k, {}), v)
                else:
                    node[k] = v
        apply(self.fake.config, patch)

    async def run_gravity(self):
        self.fake.gravity_calls += 1

    async def restart_dns(self):
        self.fake.restart_calls += 1


def make_engine(primary: FakePihole, replicas: dict, opts: SyncOptions = None):
    fakes = {"http://primary": primary}
    for name, fake in replicas.items():
        fakes[f"http://{name}"] = fake

    def factory(host, **kw):
        return FakeClient(fakes[host], host)

    primary_host = "http://primary"
    cfg = type("C", (), {})()
    cfg.primary = Node(name="primary", host=primary_host)
    cfg.primary.options = opts or SyncOptions()
    cfg.replicas = []
    for name, fake in replicas.items():
        node = Node(name=name, host=f"http://{name}")
        node.options = opts or SyncOptions()
        cfg.replicas.append(node)
    store = StateStore(tempfile.mktemp(suffix=".db"))
    return SyncEngine(cfg, store, client_factory=factory)


async def check_list_creation_query_type() -> None:
    """FTL 6.8 rejects list creation without the type query parameter."""
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth":
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(200, json={"session": {
                "valid": True, "sid": "offline-session", "validity": 1800}})
        assert request.method == "POST" and request.url.path == "/api/lists"
        ltype = request.url.params.get("type")
        if ltype not in ("block", "allow"):
            return httpx.Response(400, json={"error": {
                "message": "Specify type parameter"}})
        body = json.loads(request.content)
        assert body["type"] == ltype
        assert body["address"] == "https://example.com/" + ltype
        assert body["groups"] == [0, 2]
        assert body["comment"] == "offline list"
        assert body["enabled"] is False
        requests.append(ltype)
        return httpx.Response(201, json={"lists": [body]})

    client = PiholeClient("http://offline", password="offline")
    await client._http.aclose()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    async with client:
        for ltype in ("block", "allow"):
            await client.add_list("https://example.com/" + ltype, ltype,
                                  comment="offline list", groups=[0, 2], enabled=False)
    assert requests == ["block", "allow"]


async def run_tests() -> None:
    await check_list_creation_query_type()
    # ---- scenario 1: initial full sync --------------------------------
    primary = FakePihole()
    primary.lists = [{"address": "https://example.com/hosts", "type": "block",
                      "comment": "main", "groups": [0, 1], "enabled": True}]
    primary.groups = [{"id": 0, "name": "Default", "comment": "", "enabled": True},
                      {"id": 1, "name": "TV", "comment": "tv devices", "enabled": True}]
    primary.domains = [{"domain": "example.org", "type": "allow", "kind": "exact",
                        "comment": "", "groups": [0], "enabled": True},
                       {"domain": "(^|\\.)ads\\.net$", "type": "deny", "kind": "regex",
                        "comment": "", "groups": [0], "enabled": True}]
    primary.config["dns"]["hosts"] = ["192.168.1.50 nas.lan"]
    primary.config["dns"]["upstreams"] = ["1.1.1.1", "9.9.9.9"]

    rep = FakePihole()
    engine = make_engine(primary, {"r1": rep})
    s1 = await engine.run_cycle(reason="test", force=True)
    assert s1["ok"], s1
    r1 = s1["replicas"][0]
    assert r1.lists.added == 1 and r1.domains.added == 2 and r1.groups.added == 1, (
        r1.lists, r1.domains, r1.groups)
    assert r1.config_leaves > 0 and r1.gravity_ran and r1.restart_ran
    # group id remap: primary group 1 ("TV") -> replica id 1
    assert rep.lists[0]["groups"] == [0, 1]
    assert rep.config["dns"]["hosts"] == ["192.168.1.50 nas.lan"]
    assert rep.config["dns"]["upstreams"] == ["1.1.1.1", "9.9.9.9"]
    # secrets never leave the primary
    assert rep.config["webserver"]["api"]["password"] == "********"

    # ---- scenario 2: no-op cycle ---------------------------------------
    before = rep.dump()
    s2 = await engine.run_cycle(reason="test")
    r2 = s2["replicas"][0]
    assert r2.lists.added == 0 and r2.lists.updated == 0 and r2.domains.added == 0
    assert r2.config_leaves == 0 and not r2.gravity_ran
    assert rep.dump() == before

    # ---- scenario 3: primary changes propagate -------------------------
    primary.domains.append({"domain": "new.example", "type": "deny", "kind": "exact",
                            "comment": "", "groups": [0], "enabled": True})
    primary.config["dns"]["upstreams"] = ["9.9.9.9"]
    s3 = await engine.run_cycle(reason="test")
    r3 = s3["replicas"][0]
    assert r3.domains.added == 1 and r3.config_leaves >= 1 and r3.gravity_ran
    assert len(rep.domains) == 3
    assert rep.config["dns"]["upstreams"] == ["9.9.9.9"]

    # ---- scenario 4: delete with opt-in --------------------------------
    primary.lists.clear()
    engine.config.replicas[0].options.delete_extra_lists = True
    s4 = await engine.run_cycle(reason="test")
    r4 = s4["replicas"][0]
    assert r4.lists.removed == 1 and rep.lists == []

    # ---- scenario 5: replica drift fixed by reconcile -------------------
    rep.domains.append({"domain": "sneaky.example", "type": "allow", "kind": "exact",
                        "comment": "", "groups": [0], "enabled": True})
    engine.config.replicas[0].options.reconcile = True
    engine.config.replicas[0].options.delete_extra_domains = True
    s5 = await engine.run_cycle(reason="test")
    r5 = s5["replicas"][0]
    assert r5.domains.removed == 1
    assert not any(d["domain"] == "sneaky.example" for d in rep.domains)

    # ---- scenario 6: clients sync (gravity-sync parity) ------------------
    primary.clients = [{"client": "192.168.1.50", "comment": "nas", "groups": [1]}]
    s6 = await engine.run_cycle(reason="test")
    r6 = s6["replicas"][0]
    assert r6.clients.added == 1 and rep.clients[0]["client"] == "192.168.1.50"
    assert rep.clients[0]["groups"] == [1]  # remapped "TV" group

    # ---- helpers --------------------------------------------------------
    assert config_leaves({"a": {"b": 1}}) == {"a.b": 1}
    assert build_patch({"dns.hosts": ["x"], "misc.nice": 5}) == {
        "dns": {"hosts": ["x"]}, "misc": {"nice": 5}}

    print("pihole-sync selftest: all scenarios passed")


if __name__ == "__main__":
    asyncio.run(run_tests())