"""Entry point: wire up config, DB, poller, web UI (and simulator in demo mode).

Component map — one process, one asyncio loop:

    Wall Connector ──HTTP──▶ Poller ──writes──▶ Database (SQLite)
                               │ └─publishes──▶ EventBus ──SSE──▶ browser
                               └──POSTs──▶ notify webhook (optional, LAN)
    browser ◀──JSON/static── web.make_app ◀──reads── Database

The Poller owns all device I/O; the web layer only reads the DB (plus the
poller's in-memory status), so a busy dashboard can never add load on the
charger.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import web

from .config import parse_args
from .db import Database
from .discover import run_discovery
from .poller import EventBus, Poller
from .simulator import start_simulator
from .web import make_app

log = logging.getLogger("wallmonitor")


async def run(argv: list[str] | None = None) -> None:
    """Construct every component, serve until cancelled, unwind in order.

    Shutdown order is deliberate: stop the poller first (the only writer),
    then the web runner (readers), then the shared HTTP client, and close
    the database last so nothing can touch a closed connection."""
    cfg = parse_args(argv)
    if cfg.discover is not None:
        raise SystemExit(await run_discovery(cfg.discover, split_phase_hint=cfg.split_phase))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sim_runner = None
    if cfg.demo:
        sim_runner, sim_port = await start_simulator(split_phase=cfg.split_phase)
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
