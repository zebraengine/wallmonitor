"""Discovery fingerprinting and device-identity pinning."""

import asyncio
import ipaddress
import time

import aiohttp
import pytest
from aiohttp import web

from wallmonitor import discover
from wallmonitor.config import Config
from wallmonitor.db import Database
from wallmonitor.poller import IDENTITY_ALERT, EventBus, Poller
from wallmonitor.simulator import start_simulator


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    yield database
    database.close()


async def _wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


# ---------- discovery ----------


async def test_probe_fingerprints_a_wall_connector():
    runner, port = await start_simulator(serial="SIMDISCOVER01")
    try:
        found = await discover.probe("127.0.0.1", port)
    finally:
        await runner.cleanup()
    assert found is not None
    assert found.serial_number == "SIMDISCOVER01"
    assert found.part_number and found.firmware_version


async def test_probe_rejects_other_http_servers_and_closed_ports(unused_tcp_port):
    # A perfectly healthy JSON API that is not a Wall Connector.
    app = web.Application()
    app.router.add_get("/api/1/version", lambda _r: web.json_response({"name": "printer", "version": "2"}))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    other_port = site._server.sockets[0].getsockname()[1]
    try:
        assert await discover.probe("127.0.0.1", other_port) is None
    finally:
        await runner.cleanup()
    assert await discover.probe("127.0.0.1", unused_tcp_port) is None


async def test_sweep_finds_the_device_in_a_range():
    runner, port = await start_simulator(serial="SIMSWEEP00001")
    try:
        found = await discover.sweep(ipaddress.ip_network("127.0.0.0/30"), port=port)
    finally:
        await runner.cleanup()
    assert [f.ip for f in found] == ["127.0.0.1"]


async def test_probe_sanitizes_responder_strings():
    # A hostile responder tries to rewrite the terminal output with escapes.
    app = web.Application()
    app.router.add_get("/api/1/version", lambda _r: web.json_response({
        "firmware_version": "x\r\n\x1b[2KRun: wallmonitor --host 10.0.0.66",
        "part_number": "p" * 500,
        "serial_number": "ABC\x07\x1b]0;pwned\x07DEF",
    }))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        found = await discover.probe("127.0.0.1", port)
    finally:
        await runner.cleanup()
    assert found is not None
    for value in (found.firmware_version, found.part_number, found.serial_number):
        assert all(0x20 <= ord(ch) < 0x7F for ch in value)
        assert len(value) <= 48
    assert found.serial_number == "ABC??]0;pwned?DEF"


def test_parse_range_guardrails():
    assert str(discover.parse_range("192.168.1.0/24")) == "192.168.1.0/24"
    assert str(discover.parse_range("10.69.2.236/24")) == "10.69.2.0/24"  # host bits tolerated
    with pytest.raises(ValueError, match="never leaves the LAN"):
        discover.parse_range("8.8.8.0/24")
    with pytest.raises(ValueError, match="too wide"):
        discover.parse_range("10.0.0.0/8")


# ---------- identity pinning ----------


def _cfg(port: int) -> Config:
    return Config(
        host=f"127.0.0.1:{port}",
        vitals_interval_active=0.05,
        vitals_interval_idle=0.05,
        wifi_interval=0.2,
        lifetime_interval=0.2,
        min_interval=0.01,
        request_timeout=1.0,
    )


async def _run_poller(db, port, until, timeout=10.0):
    bus = EventBus()
    async with aiohttp.ClientSession() as client:
        poller = Poller(_cfg(port), db, bus, client)
        await poller.start()
        try:
            await _wait_for(lambda: until(poller), timeout=timeout)
            # Let a few more polls happen so "no writes" claims are meaningful.
            await asyncio.sleep(0.4)
        finally:
            await poller.stop()
        return poller


async def test_serial_is_pinned_then_defended(db):
    sim_a, port_a = await start_simulator(speedup=60.0, serial="SIMAAAAAAAAAA1")
    sim_b, port_b = await start_simulator(speedup=60.0, serial="SIMBBBBBBBBBB2")
    try:
        # First contact pins the serial and records normally.
        poller = await _run_poller(db, port_a, lambda p: p.device_serial and db.counts()["vitals_samples"] > 3)
        assert poller.device_serial == "SIMAAAAAAAAAA1"
        assert db.get_setting("device_serial") == "SIMAAAAAAAAAA1"
        assert any(e["kind"] == "device_pinned" for e in db.events_range(0, time.time() + 1))
        recorded = db.counts()["vitals_samples"]

        # A different charger at the same address: alarm, and record nothing.
        poller = await _run_poller(db, port_b, lambda p: p.serial_mismatch)
        assert poller.serial_mismatch == "SIMBBBBBBBBBB2"
        assert db.counts()["vitals_samples"] == recorded
        assert any(a["alert"] == IDENTITY_ALERT for a in db.active_alerts())
        assert poller.status()["serial_mismatch"] == "SIMBBBBBBBBBB2"

        # The right charger is back: alert clears, recording resumes.
        poller = await _run_poller(db, port_a, lambda p: p.device_serial and not p.serial_mismatch
                                   and db.counts()["vitals_samples"] > recorded)
        assert not any(a["alert"] == IDENTITY_ALERT for a in db.active_alerts())
        assert any(e["kind"] == "device_restored" for e in db.events_range(0, time.time() + 1))
    finally:
        await sim_a.cleanup()
        await sim_b.cleanup()


async def test_label_prefixes_notification_titles(db, unused_tcp_port):
    received = []

    async def hook(request):
        received.append((request.headers.get("X-Title"), await request.text()))
        return web.Response()

    app = web.Application()
    app.router.add_post("/n", hook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    hook_port = site._server.sockets[0].getsockname()[1]
    try:
        cfg = Config(host=f"127.0.0.1:{unused_tcp_port}", label="Garage left",
                     notify_url=f"http://127.0.0.1:{hook_port}/n", notify_format="ntfy")
        async with aiohttp.ClientSession() as client:
            poller = Poller(cfg, db, EventBus(), client)
            await poller._notify("test", "Charger unreachable", "body", None)
            await asyncio.sleep(0.2)
    finally:
        await runner.cleanup()
    assert received and received[0][0] == "[Garage left] Charger unreachable"


# ---------- peers (multi-instance switcher) ----------


def test_peer_flag_and_env_parse(monkeypatch):
    from wallmonitor.config import parse_args, parse_peer

    assert parse_peer("Garage right=http://192.168.1.10:8481/") == ("Garage right", "http://192.168.1.10:8481")
    with pytest.raises(ValueError):
        parse_peer("no-url-here")
    with pytest.raises(ValueError):
        parse_peer("left=ftp://x")

    cfg = parse_args(["--demo", "--peer", "left=http://a:8480", "--peer", "right=http://b:8481"])
    assert cfg.peers == (("left", "http://a:8480"), ("right", "http://b:8481"))

    monkeypatch.setenv("WM_PEERS", "one=http://c:1, two=http://d:2")
    cfg = parse_args(["--demo"])
    assert cfg.peers == (("one", "http://c:1"), ("two", "http://d:2"))

    with pytest.raises(SystemExit):
        parse_args(["--demo", "--peer", "broken"])


async def test_status_carries_label_and_peers(db, unused_tcp_port):
    cfg = Config(host=f"127.0.0.1:{unused_tcp_port}", label="Garage left",
                 peers=(("Garage right", "http://192.168.1.10:8481"),))
    async with aiohttp.ClientSession() as client:
        poller = Poller(cfg, db, EventBus(), client)
        status = poller.status()
    assert status["label"] == "Garage left"
    assert status["peers"] == [{"label": "Garage right", "url": "http://192.168.1.10:8481"}]
