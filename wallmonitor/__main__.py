"""Entry point: wire up config, DB, poller, web UI (and simulator in demo mode)."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web

from .config import parse_args
from .db import Database
from .poller import EventBus, Poller
from .simulator import start_simulator
from .web import make_app

log = logging.getLogger("wallmonitor")


async def run(argv: list[str] | None = None) -> None:
    cfg = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sim_runner = None
    if cfg.demo:
        sim_runner, sim_port = await start_simulator()
        cfg.host = f"127.0.0.1:{sim_port}"
        log.info("demo mode: simulator running at http://%s", cfg.host)

    db = Database(cfg.db_path)
    bus = EventBus()
    client = aiohttp.ClientSession()
    poller = Poller(cfg, db, bus, client)
    await poller.start()

    app = make_app(db, bus, poller)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.bind, cfg.port)
    await site.start()
    log.info("wallmonitor UI on http://%s:%d (watching Wall Connector at %s)", cfg.bind, cfg.port, cfg.host)

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await poller.stop()
        await runner.cleanup()
        if sim_runner:
            await sim_runner.cleanup()
        await client.close()
        db.close()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
