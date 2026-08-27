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


async def _maintenance(db: Database, cfg) -> None:
    """Background housekeeping: the one-time diagnostics backfill, then —
    when retention is enabled — a daily raw-JSON trim of samples older
    than the retention window. Chunked throughout, so live polling only
    ever waits a moment."""
    import time as _time

    def report(done: int, total: int) -> None:
        if done % 200_000 < 10_000 or done >= total:
            log.info("diagnostics backfill: %d / %d rows", done, total)

    touched = await asyncio.to_thread(db.backfill_diag_columns, 10_000, report)
    if touched:
        log.info("diagnostics backfill complete: %d rows", touched)
    if not cfg.retain_raw_days:
        return
    while True:
        cutoff = _time.time() - cfg.retain_raw_days * 86400.0
        counts = await asyncio.to_thread(db.trim_raw, cutoff)
        total = sum(counts.values())
        if total:
            log.info("retention: trimmed raw JSON from %d rows (%s)", total,
                     ", ".join(f"{k}={v}" for k, v in counts.items() if v))
        await asyncio.sleep(24 * 3600.0)


async def run(argv: list[str] | None = None) -> None:
    """Construct every component, serve until cancelled, unwind in order.

    Shutdown order is deliberate: stop the poller first (the only writer),
    then the web runner (readers), then the shared HTTP client, and close
    the database last so nothing can touch a closed connection."""
    cfg = parse_args(argv)
    if cfg.discover is not None:
        raise SystemExit(await run_discovery(cfg.discover, split_phase_hint=cfg.split_phase))
    if cfg.backup_dir:
        from .backup import Keep, run_backup

        result = run_backup(cfg.db_path, cfg.backup_dir, cfg.backup_compress, Keep.parse(cfg.backup_keep))
        print(f"{result.path}: {result.snapshot_bytes / 1e6:.1f} MB snapshot (integrity {result.integrity}, "
              f"{result.snapshot_s:.1f} s) -> {result.output_bytes / 1e6:.1f} MB "
              f"{cfg.backup_compress} ({result.compress_s:.1f} s)")
        if result.deleted:
            print(f"rotated out {len(result.deleted)}: " + ", ".join(result.deleted))
        raise SystemExit(0)
    if cfg.compact:
        db = Database(cfg.db_path)
        try:
            before, after = db.vacuum()
        finally:
            db.close()
        print(f"{cfg.db_path}: {before / 1e6:.1f} MB -> {after / 1e6:.1f} MB "
              f"({(before - after) / 1e6:.1f} MB reclaimed)")
        raise SystemExit(0)
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
    backfill = asyncio.create_task(_maintenance(db, cfg), name="wallmonitor-maintenance")

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
        backfill.cancel()
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
