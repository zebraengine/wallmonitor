"""SQLite storage for wallmonitor.

Every poll response is stored twice over: the complete raw JSON body from the
Wall Connector (full fidelity — nothing the device said is ever discarded) and
a set of extracted columns for fast querying/charting. All timestamps are a
single clock: host UTC epoch seconds captured the moment the response arrived.

sqlite3 is synchronous; call sites in async code wrap these methods in
asyncio.to_thread. A lock serializes access to the shared connection.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vitals_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session_id INTEGER,
    vehicle_connected INTEGER,
    contactor_closed INTEGER,
    session_s INTEGER,
    session_energy_wh REAL,
    grid_v REAL,
    grid_hz REAL,
    vehicle_current_a REAL,
    current_a_a REAL,
    current_b_a REAL,
    current_c_a REAL,
    current_n_a REAL,
    voltage_a_v REAL,
    voltage_b_v REAL,
    voltage_c_v REAL,
    pcba_temp_c REAL,
    handle_temp_c REAL,
    mcu_temp_c REAL,
    evse_state INTEGER,
    config_status INTEGER,
    uptime_s INTEGER,
    total_power_w REAL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vitals_ts ON vitals_samples(ts);
CREATE INDEX IF NOT EXISTS idx_vitals_session ON vitals_samples(session_id);

CREATE TABLE IF NOT EXISTS wifi_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    connected INTEGER,
    internet INTEGER,
    signal_strength INTEGER,
    rssi INTEGER,
    snr INTEGER,
    infra_ip TEXT,
    ssid TEXT,
    mac TEXT,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wifi_ts ON wifi_samples(ts);

CREATE TABLE IF NOT EXISTS lifetime_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    energy_wh REAL,
    charge_starts INTEGER,
    charging_time_s INTEGER,
    contactor_cycles INTEGER,
    contactor_cycles_loaded INTEGER,
    connector_cycles INTEGER,
    alert_count INTEGER,
    thermal_foldbacks INTEGER,
    avg_startup_temp REAL,
    uptime_s INTEGER,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lifetime_ts ON lifetime_samples(ts);

CREATE TABLE IF NOT EXISTS version_info (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    firmware_version TEXT,
    part_number TEXT,
    serial_number TEXT,
    git_branch TEXT,
    web_service TEXT,
    raw TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    start_ts REAL NOT NULL,
    end_ts REAL,
    energy_wh REAL,
    max_power_w REAL,
    avg_power_w REAL,
    charging_s REAL,
    sample_count INTEGER,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start_ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    alert TEXT NOT NULL,
    source TEXT NOT NULL,
    first_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    cleared_ts REAL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_alerts_active ON alerts(active);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""

VITALS_COLUMNS = {
    # db column -> key in the vitals JSON
    "vehicle_connected": "vehicle_connected",
    "contactor_closed": "contactor_closed",
    "session_s": "session_s",
    "session_energy_wh": "session_energy_wh",
    "grid_v": "grid_v",
    "grid_hz": "grid_hz",
    "vehicle_current_a": "vehicle_current_a",
    "current_a_a": "currentA_a",
    "current_b_a": "currentB_a",
    "current_c_a": "currentC_a",
    "current_n_a": "currentN_a",
    "voltage_a_v": "voltageA_v",
    "voltage_b_v": "voltageB_v",
    "voltage_c_v": "voltageC_v",
    "pcba_temp_c": "pcba_temp_c",
    "handle_temp_c": "handle_temp_c",
    "mcu_temp_c": "mcu_temp_c",
    "evse_state": "evse_state",
    "config_status": "config_status",
    "uptime_s": "uptime_s",
}


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;"
            )
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    # ---------- writes ----------

    def insert_vitals(self, ts: float, raw: dict, session_id: int | None, total_power_w: float | None) -> int:
        cols = {c: raw.get(k) for c, k in VITALS_COLUMNS.items()}
        with self._lock:
            cur = self._execute(
                f"""INSERT INTO vitals_samples
                    (ts, session_id, total_power_w, raw, {", ".join(cols)})
                    VALUES (?, ?, ?, ?, {", ".join("?" for _ in cols)})""",
                (ts, session_id, total_power_w, json.dumps(raw), *cols.values()),
            )
            return cur.lastrowid or 0

    def insert_wifi(self, ts: float, raw: dict, ssid: str | None = None) -> int:
        with self._lock:
            cur = self._execute(
                """INSERT INTO wifi_samples
                   (ts, connected, internet, signal_strength, rssi, snr, infra_ip, ssid, mac, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    raw.get("wifi_connected"),
                    raw.get("internet"),
                    raw.get("wifi_signal_strength"),
                    raw.get("wifi_rssi"),
                    raw.get("wifi_snr"),
                    raw.get("wifi_infra_ip"),
                    ssid if ssid is not None else raw.get("wifi_ssid"),
                    raw.get("wifi_mac"),
                    json.dumps(raw),
                ),
            )
            return cur.lastrowid or 0

    def insert_lifetime(self, ts: float, raw: dict) -> int:
        with self._lock:
            cur = self._execute(
                """INSERT INTO lifetime_samples
                   (ts, energy_wh, charge_starts, charging_time_s, contactor_cycles,
                    contactor_cycles_loaded, connector_cycles, alert_count, thermal_foldbacks,
                    avg_startup_temp, uptime_s, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    raw.get("energy_wh"),
                    raw.get("charge_starts"),
                    raw.get("charging_time_s"),
                    raw.get("contactor_cycles"),
                    raw.get("contactor_cycles_loaded"),
                    raw.get("connector_cycles"),
                    raw.get("alert_count"),
                    raw.get("thermal_foldbacks"),
                    raw.get("avg_startup_temp"),
                    raw.get("uptime_s"),
                    json.dumps(raw),
                ),
            )
            return cur.lastrowid or 0

    def insert_version(self, ts: float, raw: dict) -> int:
        with self._lock:
            cur = self._execute(
                """INSERT INTO version_info
                   (ts, firmware_version, part_number, serial_number, git_branch, web_service, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    raw.get("firmware_version"),
                    raw.get("part_number"),
                    raw.get("serial_number"),
                    raw.get("git_branch"),
                    raw.get("web_service"),
                    json.dumps(raw),
                ),
            )
            return cur.lastrowid or 0

    def add_event(self, ts: float, kind: str, detail: dict | None = None) -> int:
        with self._lock:
            cur = self._execute(
                "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                (ts, kind, json.dumps(detail) if detail else None),
            )
            return cur.lastrowid or 0

    def start_session(self, start_ts: float) -> int:
        with self._lock:
            cur = self._execute("INSERT INTO sessions (start_ts) VALUES (?)", (start_ts,))
            return cur.lastrowid or 0

    def close_session(self, session_id: int, end_ts: float, end_reason: str) -> None:
        """Close a session and compute its aggregates from recorded samples."""
        with self._lock:
            agg = self._conn.execute(
                """SELECT COUNT(*) AS n, MAX(total_power_w) AS max_p, AVG(total_power_w) AS avg_p,
                          MAX(session_energy_wh) AS energy
                   FROM vitals_samples WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            charging = self._conn.execute(
                """SELECT COALESCE(SUM(dt), 0) AS s FROM (
                       SELECT ts - LAG(ts) OVER (ORDER BY ts) AS dt,
                              LAG(contactor_closed) OVER (ORDER BY ts) AS prev_cc
                       FROM vitals_samples WHERE session_id = ?
                   ) WHERE prev_cc = 1 AND dt IS NOT NULL AND dt < 120""",
                (session_id,),
            ).fetchone()
            self._execute(
                """UPDATE sessions SET end_ts = ?, end_reason = ?, sample_count = ?,
                          max_power_w = ?, avg_power_w = ?, energy_wh = ?, charging_s = ?
                   WHERE id = ?""",
                (
                    end_ts,
                    end_reason,
                    agg["n"],
                    agg["max_p"],
                    agg["avg_p"],
                    agg["energy"],
                    charging["s"],
                    session_id,
                ),
            )

    def open_session_id(self) -> int | None:
        with self._lock:
            row = self._conn.execute("SELECT id FROM sessions WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            return row["id"] if row else None

    def raise_alert(self, ts: float, alert: str, source: str) -> tuple[int, bool]:
        """Mark an alert active. Returns (alert_id, newly_raised)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM alerts WHERE alert = ? AND source = ? AND active = 1", (alert, source)
            ).fetchone()
            if row:
                self._execute("UPDATE alerts SET last_ts = ? WHERE id = ?", (ts, row["id"]))
                return row["id"], False
            cur = self._execute(
                "INSERT INTO alerts (alert, source, first_ts, last_ts, active) VALUES (?, ?, ?, ?, 1)",
                (alert, source, ts, ts),
            )
            return cur.lastrowid or 0, True

    def clear_alert(self, ts: float, alert: str, source: str) -> bool:
        with self._lock:
            cur = self._execute(
                "UPDATE alerts SET active = 0, cleared_ts = ?, last_ts = ? WHERE alert = ? AND source = ? AND active = 1",
                (ts, ts, alert, source),
            )
            return cur.rowcount > 0

    def active_alerts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE active = 1 ORDER BY first_ts DESC"
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- reads ----------

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def latest_vitals(self) -> dict | None:
        rows = self._rows("SELECT * FROM vitals_samples ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def latest_wifi(self) -> dict | None:
        rows = self._rows("SELECT * FROM wifi_samples ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def latest_lifetime(self) -> dict | None:
        rows = self._rows("SELECT * FROM lifetime_samples ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def latest_version(self) -> dict | None:
        rows = self._rows("SELECT * FROM version_info ORDER BY ts DESC LIMIT 1")
        return rows[0] if rows else None

    def vitals_range(self, t_from: float, t_to: float, max_points: int = 1500) -> list[dict]:
        """Vitals samples in a range, bucket-averaged down to at most max_points."""
        n = self._rows(
            "SELECT COUNT(*) AS n FROM vitals_samples WHERE ts >= ? AND ts <= ?", (t_from, t_to)
        )[0]["n"]
        if n <= max_points:
            return self._rows(
                """SELECT ts, total_power_w, vehicle_current_a, current_a_a, current_b_a, current_c_a,
                          voltage_a_v, voltage_b_v, voltage_c_v, grid_v, grid_hz,
                          pcba_temp_c, handle_temp_c, mcu_temp_c, session_energy_wh,
                          vehicle_connected, contactor_closed, evse_state, session_id
                   FROM vitals_samples WHERE ts >= ? AND ts <= ? ORDER BY ts""",
                (t_from, t_to),
            )
        width = (t_to - t_from) / max_points
        return self._rows(
            """SELECT MIN(ts) AS ts, AVG(total_power_w) AS total_power_w, MAX(total_power_w) AS max_power_w,
                      AVG(vehicle_current_a) AS vehicle_current_a,
                      AVG(current_a_a) AS current_a_a, AVG(current_b_a) AS current_b_a,
                      AVG(current_c_a) AS current_c_a,
                      AVG(voltage_a_v) AS voltage_a_v, AVG(voltage_b_v) AS voltage_b_v,
                      AVG(voltage_c_v) AS voltage_c_v,
                      AVG(grid_v) AS grid_v, AVG(grid_hz) AS grid_hz,
                      AVG(pcba_temp_c) AS pcba_temp_c, AVG(handle_temp_c) AS handle_temp_c,
                      AVG(mcu_temp_c) AS mcu_temp_c,
                      MAX(session_energy_wh) AS session_energy_wh,
                      MAX(vehicle_connected) AS vehicle_connected,
                      MAX(contactor_closed) AS contactor_closed,
                      MAX(evse_state) AS evse_state, MAX(session_id) AS session_id
               FROM vitals_samples WHERE ts >= ? AND ts <= ?
               GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY ts""",
            (t_from, t_to, t_from, width),
        )

    def wifi_range(self, t_from: float, t_to: float, max_points: int = 1000) -> list[dict]:
        n = self._rows("SELECT COUNT(*) AS n FROM wifi_samples WHERE ts >= ? AND ts <= ?", (t_from, t_to))[0]["n"]
        if n <= max_points:
            return self._rows(
                """SELECT ts, connected, internet, signal_strength, rssi, snr
                   FROM wifi_samples WHERE ts >= ? AND ts <= ? ORDER BY ts""",
                (t_from, t_to),
            )
        width = (t_to - t_from) / max_points
        return self._rows(
            """SELECT MIN(ts) AS ts, MIN(connected) AS connected, MIN(internet) AS internet,
                      AVG(signal_strength) AS signal_strength, AVG(rssi) AS rssi, AVG(snr) AS snr
               FROM wifi_samples WHERE ts >= ? AND ts <= ?
               GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY ts""",
            (t_from, t_to, t_from, width),
        )

    def sessions_range(self, t_from: float, t_to: float) -> list[dict]:
        return self._rows(
            """SELECT * FROM sessions
               WHERE start_ts <= ? AND COALESCE(end_ts, ?) >= ?
               ORDER BY start_ts DESC""",
            (t_to, t_to, t_from),
        )

    def session(self, session_id: int) -> dict | None:
        rows = self._rows("SELECT * FROM sessions WHERE id = ?", (session_id,))
        return rows[0] if rows else None

    def alerts_range(self, t_from: float, t_to: float) -> list[dict]:
        return self._rows(
            """SELECT * FROM alerts WHERE last_ts >= ? AND first_ts <= ?
               ORDER BY active DESC, first_ts DESC""",
            (t_from, t_to),
        )

    def events_range(self, t_from: float, t_to: float, kinds: list[str] | None = None, limit: int = 2000) -> list[dict]:
        if kinds:
            marks = ",".join("?" for _ in kinds)
            return self._rows(
                f"SELECT * FROM events WHERE ts >= ? AND ts <= ? AND kind IN ({marks}) ORDER BY ts DESC LIMIT ?",
                (t_from, t_to, *kinds, limit),
            )
        return self._rows(
            "SELECT * FROM events WHERE ts >= ? AND ts <= ? ORDER BY ts DESC LIMIT ?",
            (t_from, t_to, limit),
        )

    def last_activity_ts(self) -> float | None:
        """Timestamp of the most recent recorded sample or event, if any."""
        rows = self._rows(
            """SELECT MAX(ts) AS ts FROM (
                   SELECT MAX(ts) AS ts FROM vitals_samples
                   UNION ALL SELECT MAX(ts) FROM events
               )"""
        )
        return rows[0]["ts"] if rows and rows[0]["ts"] is not None else None

    def counts(self) -> dict[str, Any]:
        return {
            "vitals_samples": self._rows("SELECT COUNT(*) AS n FROM vitals_samples")[0]["n"],
            "wifi_samples": self._rows("SELECT COUNT(*) AS n FROM wifi_samples")[0]["n"],
            "lifetime_samples": self._rows("SELECT COUNT(*) AS n FROM lifetime_samples")[0]["n"],
            "sessions": self._rows("SELECT COUNT(*) AS n FROM sessions")[0]["n"],
            "events": self._rows("SELECT COUNT(*) AS n FROM events")[0]["n"],
            "alerts": self._rows("SELECT COUNT(*) AS n FROM alerts")[0]["n"],
        }
