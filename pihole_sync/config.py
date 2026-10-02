"""Configuration loading (YAML + environment variables)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Optional

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Config paths that are never synced. Prefix matching, so "dhcp" covers the
# whole DHCP subtree. Secrets are additionally filtered by masked-value
# detection (the API returns "********" for secrets).
DEFAULT_EXCLUDES = [
    "dhcp",  # rogue DHCP servers are dangerous; opt in explicitly
    "webserver",  # web UI serving config is per-instance (paths, ports, auth)
    "files",  # host file locations (gravity db etc.)
]

# Values returned masked by the API must never be written back.
MASKED_VALUE = "********"


class ConfigError(Exception):
    pass


def _interpolate(value: Any, origin: str) -> Any:
    """Replace ${ENV_VAR} references with environment values."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            name = m.group(1)
            val = os.environ.get(name)
            if val is None:
                raise ConfigError(
                    f"{origin}: environment variable {name!r} (referenced as ${{{name}}}) is not set"
                )
            return val

        return _ENV_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _interpolate(v, origin) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v, origin) for v in value]
    return value


def _norm_host(host: str) -> str:
    host = host.strip().rstrip("/")
    if "://" not in host:
        host = "http://" + host
    return host


@dataclass
class SyncOptions:
    """Sync behaviour; a copy is derived per replica with overrides applied."""

    interval_seconds: int = 300
    run_gravity: str = "auto"  # auto | always | never
    restart_dns: str = "auto"  # auto | always | never
    sync_settings: bool = True
    sync_local_dns: bool = True
    sync_local_cnames: bool = True
    sync_static_dhcp: bool = False  # static DHCP assignments (dhcp.hosts)
    reconcile: bool = False
    delete_extra_lists: bool = False
    delete_extra_domains: bool = False
    delete_extra_groups: bool = False
    delete_extra_clients: bool = False
    exclude_config_paths: list = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    request_timeout: float = 30.0
    gravity_timeout: float = 900.0

    @staticmethod
    def _fields() -> set:
        return {f.name for f in fields(SyncOptions)}

    @classmethod
    def from_dict(cls, d: dict) -> "SyncOptions":
        d = d or {}
        allowed = cls._fields()
        unknown = [k for k in d if k not in allowed]
        if unknown:
            raise ConfigError(f"sync: unknown option(s): {', '.join(sorted(unknown))}")
        return cls(**d)

    def override(self, d: Optional[dict], origin: str) -> "SyncOptions":
        """Return a copy with per-replica overrides applied."""
        if not d:
            return self
        allowed = self._fields()
        unknown = [k for k in d if k not in allowed]
        if unknown:
            raise ConfigError(f"{origin}: unknown override(s): {', '.join(sorted(unknown))}")
        return replace(self, **d)


@dataclass
class Node:
    """A Pi-hole instance (primary or replica)."""

    name: str
    host: str
    password: str = ""
    totp: str = ""
    verify_tls: bool = True
    options: SyncOptions = field(default_factory=SyncOptions)

    @classmethod
    def from_dict(cls, d: dict, origin: str) -> "Node":
        if not isinstance(d, dict) or not d.get("host"):
            raise ConfigError(f"{origin}: 'host' is required")
        host = _norm_host(str(d["host"]))
        name = str(d.get("name") or host.split("//")[-1])
        password = d.get("password", "") or ""
        totp = d.get("totp", "") or ""
        return cls(
            name=name,
            host=host,
            password=str(password),
            totp=str(totp),
            verify_tls=bool(d.get("verify_tls", True)),
        )


@dataclass
class Config:
    sync: SyncOptions
    primary: Node
    replicas: list
    path: str = ""


def _sync_overrides(d: dict) -> dict:
    """Extract per-node sync.* overrides from a node mapping."""
    return {k: v for k, v in (d or {}).items() if k in SyncOptions._fields()}


def load_config(path: str) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"Config file not found: {p}\n"
            "Copy config.example.yaml to config.yaml and edit it."
        )
    raw = _interpolate(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, str(p))
    if not isinstance(raw, dict):
        raise ConfigError(f"{p}: expected a YAML mapping")

    sync = SyncOptions.from_dict(raw.get("sync") or {})

    primary_raw = raw.get("primary")
    if not primary_raw:
        raise ConfigError(f"{p}: 'primary' section is required")
    primary = Node.from_dict(primary_raw, "primary")
    primary.options = sync.override(_sync_overrides(primary_raw), "primary")

    replicas = []
    for i, r in enumerate(raw.get("replicas") or []):
        node = Node.from_dict(r, f"replicas[{i}]")
        origin = f"replicas[{i}] ({node.name})"
        node.options = sync.override(_sync_overrides(r), origin)
        replicas.append(node)

    return Config(sync=sync, primary=primary, replicas=replicas, path=str(p))