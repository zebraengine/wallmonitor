"""Find Tesla Wall Connector(s) on the local network.

The Gen 3 advertises nothing discoverable — no mDNS, no SSDP — and its DHCP
hostname lives only inside the router. So discovery is active: a polite
TCP sweep of port 80 across one private subnet, then a content fingerprint
of each responder via ``GET /api/1/version``. Only a Wall Connector returns
JSON carrying ``firmware_version``, ``part_number`` and ``serial_number``
together, which makes false positives practically impossible.

Guardrails, because this is a network scanner shipped in a monitoring tool:
only RFC 1918 ranges are ever swept; the default range is the host's own
/24; anything wider than /22 needs to be asked for explicitly; and each
responsive host receives exactly one sub-kilobyte request.

Tesla-assigned MAC prefixes seen in the neighbour (ARP) cache are probed
first, so on a flat network the answer usually arrives before the sweep
has really begun.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import subprocess
import sys
from dataclasses import dataclass

# IEEE OUIs registered to Tesla (cars, Powerwalls and Wall Connectors all
# draw from these). A hint for probe ordering only — identity always comes
# from the version fingerprint.
TESLA_OUIS = frozenset({"98:ED:5C", "4C:FC:AA", "54:F8:F0", "DC:44:27", "E8:E5:D6"})

FINGERPRINT_KEYS = ("firmware_version", "part_number", "serial_number")
MAX_PREFIX_DEFAULT = 22  # wider than this requires --discover with an explicit range


@dataclass(frozen=True)
class Found:
    ip: str
    firmware_version: str
    part_number: str
    serial_number: str


def local_subnet() -> ipaddress.IPv4Network | None:
    """The /24 containing this host's default-route address, or None when
    that address isn't private (the tool then refuses to guess)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # no packet is sent for UDP connect
            ip = ipaddress.ip_address(sock.getsockname()[0])
    except OSError:
        return None
    if not ip.is_private or ip.is_loopback:
        return None
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def parse_range(spec: str | None) -> ipaddress.IPv4Network:
    """Validate a user-supplied range (or pick the local subnet). Refuses
    public ranges outright and over-wide ranges unless explicit."""
    if spec is None or spec == "auto":
        net = local_subnet()
        if net is None:
            raise ValueError(
                "could not determine a private local subnet; pass one explicitly, "
                "e.g. --discover 192.168.1.0/24"
            )
        return net
    net = ipaddress.ip_network(spec, strict=False)
    if not isinstance(net, ipaddress.IPv4Network) or not net.is_private:
        raise ValueError(f"{spec} is not a private IPv4 range; discovery never leaves the LAN")
    if net.prefixlen < 16:
        raise ValueError(f"{spec} is too wide ({net.num_addresses} addresses); use /16 or narrower")
    return net


def arp_hints() -> list[str]:
    """IPs from the neighbour cache whose MAC carries a Tesla OUI.
    Best-effort: any failure just means no head start."""
    commands = (["ip", "-4", "neigh"], ["arp", "-an"])
    text = ""
    for cmd in commands:
        try:
            text = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        if text:
            break
    hints = []
    for line in text.splitlines():
        mac = re.search(r"([0-9a-f]{1,2}:[0-9a-f]{1,2}:[0-9a-f]{1,2}):", line, re.I)
        ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
        if not (mac and ip):
            continue
        oui = ":".join(part.zfill(2) for part in mac.group(1).split(":")).upper()
        if oui in TESLA_OUIS:
            hints.append(ip.group(1))
    return hints


async def probe(ip: str, port: int = 80, timeout: float = 1.5) -> Found | None:
    """One fingerprint request. Returns the device if, and only if, the
    body is the Wall Connector's version document."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=0.6)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        writer.write(
            b"GET /api/1/version HTTP/1.1\r\nHost: " + ip.encode()
            + b"\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
        # The real device answers chunked over a keep-alive connection, so
        # read until a complete JSON object is in hand rather than to EOF.
        raw = b""
        while b"}" not in raw and len(raw) < 65536:
            piece = await asyncio.wait_for(reader.read(4096), timeout=timeout)
            if not piece:
                break
            raw += piece
    except (OSError, asyncio.TimeoutError):
        return None
    finally:
        writer.close()
    start, end = raw.find(b"{"), raw.rfind(b"}")
    if start < 0 or end <= start:
        return None
    try:
        info = json.loads(raw[start : end + 1])
    except ValueError:
        return None
    if not isinstance(info, dict) or not all(isinstance(info.get(k), str) for k in FINGERPRINT_KEYS):
        return None
    return Found(ip, *(_clean(info[k]) for k in FINGERPRINT_KEYS))


def _clean(value: str, limit: int = 48) -> str:
    """Printable ASCII only, bounded length. These strings come from
    whatever answered on the LAN and go straight to the operator's
    terminal; a hostile responder must not be able to smuggle control or
    escape sequences into the output it is trying to get copy-pasted."""
    return "".join(ch if 0x20 <= ord(ch) < 0x7F else "?" for ch in value)[:limit]


async def sweep(net: ipaddress.IPv4Network, port: int = 80, concurrency: int = 128,
                hints: list[str] | None = None) -> list[Found]:
    """Probe every host in the range (hinted addresses first); return all
    Wall Connectors found, sorted by address."""
    sem = asyncio.Semaphore(concurrency)

    async def guarded(ip: str) -> Found | None:
        async with sem:
            return await probe(ip, port)

    ordered = list(dict.fromkeys((hints or []) + [str(h) for h in net.hosts()]))
    in_range = [ip for ip in ordered if ipaddress.ip_address(ip) in net]
    results = await asyncio.gather(*(guarded(ip) for ip in in_range))
    found = [r for r in results if r is not None]
    return sorted(found, key=lambda f: ipaddress.ip_address(f.ip))


async def run_discovery(spec: str | None, split_phase_hint: bool = False) -> int:
    """The --discover command: sweep, print what was found and the exact
    command to run next. Exit status 0 when at least one device answered."""
    try:
        net = parse_range(spec)
    except ValueError as ex:
        print(f"discover: {ex}", file=sys.stderr)
        return 2
    hints = arp_hints()
    print(f"Scanning {net} for Wall Connectors"
          + (f" (checking {len(hints)} Tesla-MAC neighbour(s) first)" if hints else "") + " …")
    found = await sweep(net, hints=hints)
    if not found:
        print("No Wall Connector answered on this range.")
        print("If the charger lives on another subnet or VLAN, pass that range: "
              "wallmonitor --discover 192.168.2.0/24")
        return 1
    flag = " --split-phase" if split_phase_hint else ""
    print(f"Found {len(found)} Wall Connector{'s' if len(found) > 1 else ''}:")
    for dev in found:
        print(f"  {dev.ip:<15}  firmware {dev.firmware_version}  part {dev.part_number}"
              f"  serial …{dev.serial_number[-6:]}")
    print()
    if len(found) == 1:
        print(f"Run:  wallmonitor --host {found[0].ip}{flag}")
    else:
        print("Run one instance per charger, each with its own --db and --port, e.g.:")
        for i, dev in enumerate(found):
            print(f"  wallmonitor --host {dev.ip}{flag} --label wc-{i + 1} "
                  f"--db wc-{i + 1}.db --port {8480 + i}")
    return 0
