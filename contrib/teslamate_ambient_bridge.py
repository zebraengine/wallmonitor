#!/usr/bin/env python3
"""Bridge a TeslaMate-logged vehicle's ambient sensor into wallmonitor.

A car parked in the garage carries a real outside-air thermometer, and
TeslaMate already records it (positions.outside_temp while parked awake,
charges.outside_temp while charging). This script reads the newest such
value from TeslaMate's Postgres and POSTs it to wallmonitor's
/api/ambient tagged ``source: "car"`` — the tier the thermal model uses
only when no stationary sensor is reporting.

The point of the bridge is *when it stays silent*. It posts nothing when:

- the newest reading is stale (car asleep, offline, or TeslaMate down) —
  wallmonitor's freshness window then expires and every consumer falls
  back to the handle proxy on its own;
- a drive is in progress, or one ended less than --drive-cooldown-s ago
  (the sensor housing heat-soaks while driving and reads high for a
  while afterward);
- the car cannot be placed in the garage: within --geofence (a TeslaMate
  geofence name) or --home-lat/--home-lon/--home-radius-m when either is
  configured, else the fallback gate — a vehicle currently plugged into
  the Wall Connector wallmonitor watches is, by definition, home;
- the newest reading was already posted (state file), so a parked car
  that stops updating does not masquerade as a live thermometer.

Stdlib only; TeslaMate's Postgres is reached through ``docker exec`` on
the compose container, so nothing about the TeslaMate stack changes and
no port needs publishing. Run it from cron or a systemd timer (see
deploy/install-teslamate-bridge.sh); one invocation reads, decides,
optionally posts, and exits.

Example:
    ./teslamate_ambient_bridge.py --car-id 1 --geofence Home --dry-run
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

TEMP_MIN_C, TEMP_MAX_C = -40.0, 85.0


@dataclass
class Reading:
    """One outside-temperature report from TeslaMate: when, what, and where
    (position may be absent — TeslaMate doesn't attach one to every row)."""

    ts: float
    temp_c: float
    lat: float | None
    lon: float | None


@dataclass
class Config:
    """Gating parameters for decide(); the defaults encode the physics:
    drive_cooldown_s covers the sensor housing's post-drive heat soak, and
    max_age_s bounds how stale a parked car's last report may be."""

    car_id: int
    max_age_s: float = 600.0
    drive_cooldown_s: float = 2700.0
    home: tuple[float, float] | None = None  # (lat, lon)
    home_radius_m: float = 150.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters — the am-I-home test."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(a))


def decide(
    now: float,
    readings: list[Reading],
    last_drive_end: float,
    drive_in_progress: bool,
    plugged_in: bool,
    last_posted_ts: float,
    cfg: Config,
) -> tuple[Reading | None, str]:
    """Pure gating logic: the newest admissible reading, or (None, why)."""
    fresh = [r for r in readings if r is not None]
    if not fresh:
        return None, "no telemetry for this car yet"
    newest = max(fresh, key=lambda r: r.ts)
    if now - newest.ts > cfg.max_age_s:
        return None, f"newest reading is {now - newest.ts:.0f}s old (car asleep or away?)"
    if drive_in_progress:
        return None, "drive in progress"
    if last_drive_end and now - last_drive_end < cfg.drive_cooldown_s:
        left = cfg.drive_cooldown_s - (now - last_drive_end)
        return None, f"post-drive cooldown ({left:.0f}s left; sensor heat-soaks while driving)"
    if cfg.home is not None:
        if newest.lat is None or newest.lon is None:
            return None, "home is configured but the reading carries no position fix"
        dist = haversine_m(newest.lat, newest.lon, cfg.home[0], cfg.home[1])
        if dist > cfg.home_radius_m:
            return None, f"car is {dist:.0f}m from home (> {cfg.home_radius_m:.0f}m)"
    elif not plugged_in:
        return None, "no home location configured and nothing plugged into the Wall Connector"
    if not (TEMP_MIN_C <= newest.temp_c <= TEMP_MAX_C):
        return None, f"implausible temperature {newest.temp_c} C"
    if newest.ts <= last_posted_ts:
        return None, "already posted this reading"
    return newest, "ok"


# ---------------------------------------------------------------------------
# TeslaMate access (psql through docker exec; -t -A -F| output)


def _psql(psql_cmd: list[str], sql: str) -> list[list[str]]:
    """Run one statement through the configured psql command (docker exec
    by default); rows come back as lists of pipe-split strings."""
    out = subprocess.run(
        psql_cmd + ["-t", "-A", "-F", "|", "-c", sql],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    return [line.split("|") for line in out.splitlines() if line.strip()]


def _reading_rows(rows: list[list[str]]) -> Reading | None:
    """First parseable row as a Reading; position columns are optional."""
    for row in rows:
        try:
            ts, temp = float(row[0]), float(row[1])
        except (ValueError, IndexError):
            continue
        lat = float(row[2]) if len(row) > 2 and row[2] else None
        lon = float(row[3]) if len(row) > 3 and row[3] else None
        return Reading(ts, temp, lat, lon)
    return None


def fetch_readings(psql_cmd: list[str], car_id: int) -> list[Reading | None]:
    """Newest parked reading and newest in-charge reading, either may be None.

    TeslaMate stores naive-UTC timestamps; extract(epoch from ...) on them
    is therefore already a Unix epoch.
    """
    parked = _reading_rows(_psql(psql_cmd, (
        "SELECT extract(epoch from date), outside_temp, latitude, longitude "
        f"FROM positions WHERE car_id = {car_id} AND outside_temp IS NOT NULL "
        "ORDER BY date DESC LIMIT 1;"
    )))
    charging = _reading_rows(_psql(psql_cmd, (
        "SELECT extract(epoch from c.date), c.outside_temp, p.latitude, p.longitude "
        "FROM charges c JOIN charging_processes cp ON c.charging_process_id = cp.id "
        "LEFT JOIN positions p ON cp.position_id = p.id "
        f"WHERE cp.car_id = {car_id} AND c.outside_temp IS NOT NULL "
        "ORDER BY c.date DESC LIMIT 1;"
    )))
    return [parked, charging]


def fetch_drive_state(psql_cmd: list[str], car_id: int) -> tuple[float, bool]:
    """(epoch of the last drive's end, drive in progress now). An open
    drive older than 6 h is presumed a crashed recording, not a drive."""
    rows = _psql(psql_cmd, (
        "SELECT coalesce(extract(epoch from max(end_date)), 0), "
        "count(*) FILTER (WHERE end_date IS NULL AND start_date > now() - interval '6 hours') "
        f"FROM drives WHERE car_id = {car_id};"
    ))
    if not rows:
        return 0.0, False
    return float(rows[0][0]), int(rows[0][1]) > 0


def fetch_geofence(psql_cmd: list[str], name: str) -> tuple[float, float, float]:
    """(lat, lon, radius_m) of a named TeslaMate geofence — how home stays
    on-box instead of in a flag, a repo, or this chat."""
    quoted = name.replace("'", "''")
    rows = _psql(psql_cmd, (
        "SELECT latitude, longitude, radius FROM geofences "
        f"WHERE name = '{quoted}' LIMIT 1;"
    ))
    if not rows:
        raise SystemExit(f"error: no TeslaMate geofence named {name!r}")
    return float(rows[0][0]), float(rows[0][1]), float(rows[0][2])


# ---------------------------------------------------------------------------
# wallmonitor access


def wallmonitor_plugged_in(base_url: str) -> bool:
    """Fallback home test when no geofence/coords are configured: a car
    plugged into this Wall Connector is, by definition, home."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/status", timeout=10) as resp:
            status = json.load(resp)
        return bool((status.get("vitals") or {}).get("vehicle_connected"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return False


def post_reading(base_url: str, temp_c: float) -> dict:
    """POST one reading to /api/ambient as source "car" (the tier a
    stationary sensor outranks). Position never leaves this process."""
    req = urllib.request.Request(
        f"{base_url}/api/ambient",
        data=json.dumps({"temp_c": round(temp_c, 2), "source": "car"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--car-id", type=int, required=True,
                        help="TeslaMate car id (cars table)")
    parser.add_argument("--wallmonitor", default="http://127.0.0.1:8480",
                        help="wallmonitor base URL (default %(default)s)")
    parser.add_argument("--psql-cmd",
                        default="docker exec teslamate-database-1 psql -U teslamate teslamate",
                        help="command that reaches TeslaMate's psql (default %(default)s)")
    parser.add_argument("--geofence", default=None,
                        help="TeslaMate geofence name that means 'home'")
    parser.add_argument("--home-lat", type=float, default=None,
                        help="home latitude (alternative to --geofence; kept out of any repo)")
    parser.add_argument("--home-lon", type=float, default=None)
    parser.add_argument("--home-radius-m", type=float, default=150.0)
    parser.add_argument("--max-age-s", type=float, default=600.0,
                        help="ignore readings older than this (default %(default)s)")
    parser.add_argument("--drive-cooldown-s", type=float, default=2700.0,
                        help="skip readings this soon after a drive (default 45 min)")
    parser.add_argument("--state-file", default="/tmp/teslamate_ambient_bridge.last",
                        help="remembers the last posted reading's timestamp")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the decision without posting")
    args = parser.parse_args(argv)

    if (args.home_lat is None) != (args.home_lon is None):
        parser.error("--home-lat and --home-lon must be given together")

    psql_cmd = shlex.split(args.psql_cmd)
    home = None
    radius = args.home_radius_m
    if args.geofence:
        lat, lon, radius = fetch_geofence(psql_cmd, args.geofence)
        home = (lat, lon)
    if args.home_lat is not None:
        home = (args.home_lat, args.home_lon)
        radius = args.home_radius_m

    cfg = Config(car_id=args.car_id, max_age_s=args.max_age_s,
                 drive_cooldown_s=args.drive_cooldown_s,
                 home=home, home_radius_m=radius)

    try:
        readings = fetch_readings(psql_cmd, cfg.car_id)
        last_drive_end, driving = fetch_drive_state(psql_cmd, cfg.car_id)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"skip: cannot query TeslaMate ({exc})")
        return 0  # transient; the timer will try again

    plugged = False if home is not None else wallmonitor_plugged_in(args.wallmonitor)

    try:
        with open(args.state_file) as fh:
            last_posted = float(fh.read().strip() or 0)
    except (OSError, ValueError):
        last_posted = 0.0

    now = time.time()
    reading, reason = decide(now, readings, last_drive_end, driving, plugged,
                             last_posted, cfg)
    if reading is None:
        print(f"skip: {reason}")
        return 0
    if args.dry_run:
        print(f"would post: {reading.temp_c:.1f} C (read {now - reading.ts:.0f}s ago)")
        return 0
    try:
        result = post_reading(args.wallmonitor, reading.temp_c)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"error: wallmonitor POST failed ({exc})", file=sys.stderr)
        return 1
    with open(args.state_file, "w") as fh:
        fh.write(repr(reading.ts))
    print(f"posted: {reading.temp_c:.1f} C as source={result.get('source')} "
          f"(read {now - reading.ts:.0f}s ago)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
