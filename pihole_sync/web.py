"""HTTP dashboard + JSON API (FastAPI)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .scheduler import Scheduler

STATIC_DIR = Path(__file__).parent / "static"


def create_app(engine, scheduler: Scheduler) -> FastAPI:
    app = FastAPI(title="pihole-sync", version=__version__, docs_url=None, redoc_url=None)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/api/status")
    async def status() -> JSONResponse:
        cfg = engine.config
        state = engine.state
        last_cycle = engine.last_cycle or {}
        replicas = []
        for r in cfg.replicas:
            last_res = None
            for res in reversed(last_cycle.get("replicas") or []):
                if res.name == r.name:
                    last_res = res
                    break
            applied = state.applied_snapshot(r.name)
            replicas.append({
                "name": r.name,
                "host": r.host,
                "in_sync": bool(applied),
                "status": ("running" if engine.running else
                           ("error" if last_res and not last_res.ok else
                            ("ok" if last_res else "pending"))),
                "error": last_res.error if last_res else "",
                "last_sync_ts": last_cycle.get("ts") if last_res else None,
                "duration": last_res.duration if last_res else None,
                "changes": _result_view(last_res),
                "gravity_ran": bool(last_res and last_res.gravity_ran),
                "restart_ran": bool(last_res and last_res.restart_ran),
            })
        primary_view = {
            "name": cfg.primary.name,
            "host": cfg.primary.host,
            "status": ("error" if last_cycle.get("primary_ok") is False else
                       ("ok" if engine.last_primary_snapshot else "pending")),
            "error": last_cycle.get("primary_error", ""),
            "version": _version_view(last_cycle.get("version")),
            "counts": _counts_view(engine.last_primary_snapshot),
        }
        return JSONResponse({
            "app": {"version": __version__, "interval_seconds": scheduler.interval},
            "running": engine.running,
            "primary": primary_view,
            "replicas": replicas,
            "last_cycle": _last_cycle_view(last_cycle),
            "history": (state.get("history", []) or [])[-25:][::-1],
            "now": time.time(),
        })

    @app.post("/api/sync")
    async def trigger_sync() -> JSONResponse:
        if scheduler.request_sync():
            return JSONResponse({"started": True})
        return JSONResponse({"started": False, "error": "sync already running"}, status_code=409)

    return app


def _result_view(res) -> dict:
    if res is None:
        return {}
    return {
        "groups": res.groups.as_dict(),
        "lists": res.lists.as_dict(),
        "domains": res.domains.as_dict(),
        "clients": res.clients.as_dict(),
        "config_leaves": res.config_leaves,
    }


def _version_view(v) -> Optional[str]:
    if not v:
        return None
    try:
        parts = []
        for section in ("core", "ftl", "web"):
            node = v.get(section) if isinstance(v, dict) else None
            local = (node or {}).get("local", {}) if isinstance(node, dict) else {}
            ver = (local or {}).get("version")
            if ver:
                parts.append(f"{section}: {ver}")
        return " | ".join(parts) or str(v)
    except Exception:
        return str(v)


def _counts_view(snap) -> dict:
    if not snap:
        return {}
    return {
        "groups": len(snap.get("groups") or []),
        "lists": len(snap.get("lists") or []),
        "domains": len(snap.get("domains") or []),
        "clients": len(snap.get("clients") or []),
    }


def _last_cycle_view(cycle: dict) -> Optional[dict]:
    if not cycle:
        return None
    out = dict(cycle)
    out["replicas"] = [
        {
            "name": r.name, "host": r.host, "ok": r.ok, "error": r.error,
            "duration": r.duration,
            "changes": _result_view(r),
            "summary": r.changes_summary(),
            "gravity_ran": r.gravity_ran, "restart_ran": r.restart_ran,
        }
        for r in (cycle.get("replicas") or [])
    ]
    return out