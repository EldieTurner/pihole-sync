"""Entrypoint: config -> engine + scheduler + dashboard."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from . import __version__
from .config import ConfigError, load_config
from .scheduler import Scheduler
from .state import StateStore
from .sync import SyncEngine
from .web import create_app

log = logging.getLogger("pihole_sync")


def _parse_args(argv=None):
    p = argparse.ArgumentParser(prog="pihole-sync", description=__doc__)
    p.add_argument("--config", default=os.environ.get("PIHOLE_SYNC_CONFIG", "/config/config.yaml"))
    p.add_argument("--state", default=os.environ.get("PIHOLE_SYNC_STATE", "/data/state.db"))
    p.add_argument("--host", default=os.environ.get("PIHOLE_SYNC_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PIHOLE_SYNC_PORT", "8080")))
    p.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    p.add_argument("--validate", action="store_true",
                   help="Load the config, print a summary and exit (passwords are not printed)")
    p.add_argument("--once", action="store_true",
                   help="Run a single sync cycle and exit (no dashboard)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        log.error("%s", e)
        return 2

    if args.validate:
        print(f"pihole-sync {__version__} - config OK: {args.config}")
        print(f"  primary:   {cfg.primary.host} ({cfg.primary.name}) password={'set' if cfg.primary.password else 'NOT SET'}")
        for r in cfg.replicas:
            print(f"  replica:   {r.host} ({r.name}) password={'set' if r.password else 'NOT SET'}")
        print(f"  interval:  {cfg.sync.interval_seconds}s | gravity: {cfg.sync.run_gravity} | "
              f"restart: {cfg.sync.restart_dns} | settings: {cfg.sync.sync_settings}")
        return 0

    state = StateStore(args.state)
    engine = SyncEngine(cfg, state)

    if args.once:
        summary = asyncio.run(engine.run_cycle(reason="cli", force=True))
        if not summary.get("ok"):
            return 1
        return 0

    scheduler = Scheduler(engine, cfg.sync.interval_seconds)
    app = create_app(engine, scheduler)

    async def _serve() -> None:
        server = uvicorn.Server(
            uvicorn.Config(app, host=args.host, port=args.port, log_level="warning", access_log=False)
        )
        sched_task = asyncio.create_task(scheduler.run(), name="pihole-sync-scheduler")
        try:
            await server.serve()
        finally:
            await scheduler.stop()
            sched_task.cancel()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    return 0