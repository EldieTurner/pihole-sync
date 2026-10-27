"""Pi-hole v6 REST API client.

Endpoint shapes were verified against the official FTL v6.2 API specs
(docs.pi-hole.net / FTL repo) and the v6 web UI's actual usage:

- POST /api/auth {"password": ...}  -> {"session": {"sid": ...}}
  Session sent via `X-FTL-SID` header. Application passwords (which bypass
  TOTP) are used as `password`.
- GET/POST /api/groups, PUT/DELETE /api/groups/{name},
  POST /api/groups:batchDelete  [{"item": name}]
- POST /api/lists?type={block|allow} {"address": ..., "type": block|allow, "comment", "groups",
  "enabled"}; PUT /api/lists/{address}?type=...; POST /api/lists:batchDelete
  [{"item": address, "type": ...}]
- POST /api/domains/{type}/{kind} {"domain": ...}; PUT
  /api/domains/{type}/{kind}/{domain}; POST /api/domains:batchDelete
  [{"item": domain, "type": ..., "kind": ...}]
- GET /api/config -> {"config": {...}} (secrets masked as "********");
  PATCH /api/config {"config": {partial tree}}
- POST /api/action/gravity, POST /api/action/restartdns
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import struct
import time
import urllib.parse
from typing import Any, Optional

import httpx

log = logging.getLogger("pihole_sync.api")


class PiholeError(Exception):
    pass


class AuthError(PiholeError):
    pass


def _totp(secret_b32: str, step: int = 30, digits: int = 6) -> str:
    """Generate a TOTP code from a base32 secret (RFC 6238)."""
    key = base64.b32decode(secret_b32.strip().replace(" ", ""), casefold=True)
    counter = int(time.time()) // step
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


class PiholeClient:
    """Talks to one Pi-hole instance. Create one per host, reuse per cycle."""

    def __init__(
        self,
        host: str,
        password: str = "",
        totp: str = "",
        verify_tls: bool = True,
        timeout: float = 30.0,
    ):
        self.base_url = host.rstrip("/")
        self.password = password
        self.totp_secret = totp
        self._http = httpx.AsyncClient(verify=verify_tls, timeout=timeout)
        self.sid: Optional[str] = None
        self.valid_until = 0.0

    async def __aenter__(self) -> "PiholeClient":
        await self.ensure_session()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        # Free the API session seat immediately (FTL limits concurrent sessions).
        if self.sid:
            try:
                await self._http.delete(
                    f"{self.base_url}/api/auth",
                    headers={"X-FTL-SID": self.sid},
                )
            except httpx.HTTPError:
                pass
            self.sid = None
        await self._http.aclose()

    # ------------------------------------------------------------------ auth
    async def ensure_session(self) -> None:
        if self.sid and self.valid_until > time.time() + 5:
            return
        body: dict = {}
        if self.password:
            body["password"] = self.password
        if self.totp_secret:
            body["totp"] = _totp(self.totp_secret)
        try:
            resp = await self._http.post(f"{self.base_url}/api/auth", json=body)
        except httpx.HTTPError as e:
            raise PiholeError(f"{self.base_url}: unreachable ({e})") from e
        if resp.status_code == 401:
            raise AuthError(
                f"{self.base_url}: authentication failed (wrong password or missing TOTP). "
                "Tip: use an application password (Settings -> API), which bypasses 2FA."
            )
        if resp.status_code >= 400:
            raise PiholeError(f"{self.base_url}: login failed ({resp.status_code}): {_err(resp)}")
        session = (resp.json() or {}).get("session") or {}
        if not session.get("valid"):
            raise AuthError(f"{self.base_url}: session not valid: {session.get('message', '?')}")
        self.sid = session.get("sid")
        # Login response carries the remaining lifetime as "validity" (relative
        # seconds); session listings use "valid_until" (absolute epoch).
        vu = session.get("valid_until")
        if vu is None:
            vu = time.time() + float(session.get("validity") or 0)
        vu = float(vu)
        self.valid_until = (time.time() + vu) if vu < 1_000_000_000 else vu
        # Conservative floor so a missing/odd value still reuses the session;
        # the 401 retry path re-authenticates if it actually expires early.
        if self.valid_until <= time.time() + 5:
            self.valid_until = time.time() + 1200

    # --------------------------------------------------------------- request
    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
        timeout: Optional[float] = None,
        _retried: bool = False,
    ) -> Any:
        await self.ensure_session()
        headers = {}
        if self.sid:
            headers["X-FTL-SID"] = self.sid
        try:
            resp = await self._http.request(
                method,
                f"{self.base_url}/api{path}",
                json=json,
                params=params,
                headers=headers,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise PiholeError(f"{self.base_url}: {method} /api{path} failed ({e})") from e
        if resp.status_code == 401 and not _retried:
            # Session expired/invalidated server-side -> re-login once and retry.
            self.sid = None
            await self.ensure_session()
            return await self.request(method, path, json=json, params=params, timeout=timeout, _retried=True)
        if resp.status_code >= 400:
            raise PiholeError(f"{self.base_url}: {method} /api{path} -> {resp.status_code}: {_err(resp)}")
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # --------------------------------------------------------------- getters
    async def get_groups(self) -> list:
        return (await self.request("GET", "/groups")).get("groups", [])

    async def get_lists(self) -> list:
        return (await self.request("GET", "/lists")).get("lists", [])

    async def get_domains(self) -> list:
        return (await self.request("GET", "/domains")).get("domains", [])

    async def get_config(self) -> dict:
        return (await self.request("GET", "/config")).get("config", {})

    async def get_clients(self) -> list:
        return (await self.request("GET", "/clients")).get("clients", [])

    async def add_client(self, client: str, comment: str = "", groups: Optional[list] = None) -> Any:
        body = {"client": client, "comment": comment}
        if groups is not None:
            body["groups"] = groups
        return await self.request("POST", "/clients", json=body)

    async def update_client(self, client: str, comment: str = "", groups: Optional[list] = None) -> Any:
        body = {"comment": comment}
        if groups is not None:
            body["groups"] = groups
        return await self.request(
            "PUT", f"/clients/{urllib.parse.quote(client, safe='')}", json=body
        )

    async def delete_clients(self, clients: list) -> Any:
        """clients: list of client identifiers (IP, MAC, name, or interface)."""
        return await self.request("POST", "/clients:batchDelete", json=[{"item": c} for c in clients])

    async def get_version(self) -> Any:
        try:
            return (await self.request("GET", "/info/version")).get("version")
        except PiholeError:
            return None

    # --------------------------------------------------------------- groups
    async def add_group(self, name: str, comment: str = "", enabled: bool = True) -> Any:
        body = {"name": name, "comment": comment, "enabled": bool(enabled)}
        return await self.request("POST", "/groups", json=body)

    async def update_group(self, name: str, comment: str = "", enabled: bool = True) -> Any:
        body = {"comment": comment, "enabled": bool(enabled)}
        return await self.request("PUT", f"/groups/{urllib.parse.quote(name, safe='')}", json=body)

    async def delete_groups(self, names: list) -> Any:
        payload = [{"item": n} for n in names]
        return await self.request("POST", "/groups:batchDelete", json=payload)

    # ---------------------------------------------------------------- lists
    async def add_list(
        self, address: str, ltype: str, comment: str = "", groups: Optional[list] = None, enabled: bool = True
    ) -> Any:
        body = {"address": address, "type": ltype, "comment": comment, "enabled": bool(enabled)}
        if groups is not None:
            body["groups"] = groups
        return await self.request("POST", "/lists", params={"type": ltype}, json=body)

    async def update_list(
        self, address: str, ltype: str, comment: str = "", groups: Optional[list] = None, enabled: bool = True
    ) -> Any:
        body = {"type": ltype, "comment": comment, "enabled": bool(enabled)}
        if groups is not None:
            body["groups"] = groups
        return await self.request(
            "PUT",
            f"/lists/{urllib.parse.quote(address, safe='')}",
            json=body,
            params={"type": ltype},
        )

    async def delete_lists(self, items: list) -> Any:
        """items: [{"item": address, "type": "block"|"allow"}]"""
        return await self.request("POST", "/lists:batchDelete", json=items)

    # -------------------------------------------------------------- domains
    async def add_domain(self, dtype: str, kind: str, domain: str, comment: str = "",
                         groups: Optional[list] = None, enabled: bool = True) -> Any:
        body = {"domain": domain, "comment": comment, "enabled": bool(enabled)}
        if groups is not None:
            body["groups"] = groups
        return await self.request("POST", f"/domains/{dtype}/{kind}", json=body)

    async def update_domain(self, dtype: str, kind: str, domain: str, comment: str = "",
                            groups: Optional[list] = None, enabled: bool = True) -> Any:
        body = {"comment": comment, "enabled": bool(enabled)}
        if groups is not None:
            body["groups"] = groups
        return await self.request(
            "PUT",
            f"/domains/{dtype}/{kind}/{urllib.parse.quote(domain, safe='')}",
            json=body,
        )

    async def delete_domains(self, items: list) -> Any:
        """items: [{"item": domain, "type": ..., "kind": ...}]"""
        return await self.request("POST", "/domains:batchDelete", json=items)

    # --------------------------------------------------------------- config
    async def patch_config(self, patch: dict) -> Any:
        """patch: nested partial config, e.g. {"dns": {"hosts": [...]}}"""
        return await self.request("PATCH", "/config", json={"config": patch})

    # --------------------------------------------------------------- actions
    async def run_gravity(self) -> Any:
        # `pihole -g` runs remotely; allow a long read timeout.
        return await self.request("POST", "/action/gravity", timeout=900.0)

    async def restart_dns(self) -> Any:
        return await self.request("POST", "/action/restartdns", timeout=180.0)


def _err(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error") or {}
        msg = err.get("message") or data
        return f"{resp.status_code}: {msg}"
    except ValueError:
        return f"{resp.status_code}: {resp.text[:200]}"