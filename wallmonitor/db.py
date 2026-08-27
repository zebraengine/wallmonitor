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

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS forecast_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    session_id INTEGER,
    basis TEXT,
    steady_state_c REAL,
    will_trip INTEGER,
    minutes_to_trip REAL,
    trip_ts REAL,
    suggested_max_a REAL,
    handle_c REAL,
    current_a REAL,
    tau_min REAL,
    fit_rmse_c REAL,
    trip_c REAL,
    raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forecast_ts ON forecast_samples(ts);

CREATE TABLE IF NOT EXISTS ambient_samples (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    temp_c REAL NOT NULL,
    humidity_pct REAL,
    pressure_hpa REAL,
    source TEXT,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS idx_ambient_ts ON ambient_samples(ts);
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
    # J1772 handshake / relay diagnostics. Added as columns later than the
    # rest (they used to be read out of the raw JSON on every query, which
    # made long sessions slow to chart); rows from before the column existed
    # are backfilled by backfill_diag_columns and served via COALESCE until
    # then.
    "pilot_high_v": "pilot_high_v",
    "pilot_low_v": "pilot_low_v",
    "prox_v": "prox_v",
    "relay_k1_v": "relay_k1_v",
    "relay_k2_v": "relay_k2_v",
}

# Columns added after the table was first shipped: ALTER TABLE'd in on
# open when missing (CREATE TABLE IF NOT EXISTS can't add them).
VITALS_LATER_COLUMNS = ("pilot_high_v", "pilot_low_v", "prox_v", "relay_k1_v", "relay_k2_v")
DIAG_BACKFILL_SETTING = "diag_backfill_done"


def _round_rows(rows: list[dict], digits: int = 2) -> list[dict]:
    """Trim float noise before JSON: bucket averages come out with 15
    digits, which roughly doubles the payload for no visible difference
    on a chart. Timestamps keep full precision."""
    for row in rows:
        for key, value in row.items():
            if key != "ts" and isinstance(value, float):
                row[key] = round(value, digits)
    return rows


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: str):
        """Open (creating if needed) the database and apply the schema.

        WAL journaling lets the web layer read while the poller writes
        without either blocking; synchronous=NORMAL is durable against an
        app crash (not an OS/power loss mid-checkpoint — acceptable for
        telemetry). The schema is pure CREATE IF NOT EXISTS, re-run on
        every startup: "migrations" are additive statements appended here.
        """
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON;"
            )
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()
        # Once every row carries the diagnostics columns, range queries stop
        # mentioning raw entirely — merely referencing the blob column makes
        # SQLite read it for every row, which is most of what made long
        # sessions slow to chart.
        self._diag_backfilled = self.get_setting(DIAG_BACKFILL_SETTING) == "1"

    def _migrate(self) -> None:
        """Additive column migrations (caller holds the lock)."""
        have = {row[1] for row in self._conn.execute("PRAGMA table_info(vitals_samples)")}
        for col in VITALS_LATER_COLUMNS:
            if col not in have:
                self._conn.execute(f"ALTER TABLE vitals_samples ADD COLUMN {col} REAL")
        self._conn.commit()

    # Tables whose raw JSON blob is trimmed by the retention policy. The
    # extracted columns stay forever; forecast_samples and version_info are
    # exempt (tiny, and their raw payloads carry fields with no column).
    RAW_TRIM_TABLES = ("vitals_samples", "wifi_samples", "lifetime_samples", "ambient_samples")

    def trim_raw(self, cutoff_ts: float, chunk: int = 10_000) -> dict[str, int]:
        """Retention: blank the raw JSON on samples older than cutoff_ts.

        Columns are untouched, so charts, fits and the degradation watch see
        exactly the same history — what is given up is re-interpreting old
        rows for fields that were never extracted. Vitals are only trimmed
        once the diagnostics backfill has finished (its json_extract source
        is the raw blob). A per-table timestamp cursor in settings makes the
        daily run scan only rows that newly aged past the cutoff, and the
        chunked updates hold the write lock briefly each. Freed pages are
        reused by new inserts, so the file stops growing; a one-shot VACUUM
        (--compact) reclaims the space for the filesystem.
        """
        counts: dict[str, int] = {}
        for table in self.RAW_TRIM_TABLES:
            if table == "vitals_samples" and self.get_setting(DIAG_BACKFILL_SETTING) != "1":
                continue
            cursor_key = f"raw_trim_ts:{table}"
            start_ts = float(self.get_setting(cursor_key) or 0.0)
            trimmed = 0
            while True:
                with self._lock:
                    rows = self._conn.execute(
                        f"SELECT id, ts FROM {table} WHERE ts >= ? AND ts < ? "
                        "ORDER BY ts LIMIT ?",
                        (start_ts, cutoff_ts, chunk),
                    ).fetchall()
                    if not rows:
                        break
                    ids = [row[0] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    cur = self._execute(
                        f"UPDATE {table} SET raw = '' WHERE id IN ({placeholders}) AND raw != ''",
                        tuple(ids),
                    )
                    trimmed += cur.rowcount
                    start_ts = rows[-1][1]
                    self._execute(
                        "INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (cursor_key, repr(start_ts)),
                    )
                if len(rows) < chunk:
                    break
            counts[table] = trimmed
        return counts

    def vacuum(self) -> tuple[int, int]:
        """VACUUM, returning (bytes_before, bytes_after). Exclusive — run
        while nothing else is writing (the --compact command exists so this
        never happens implicitly under a live poller)."""
        import os

        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = os.path.getsize(self.path)
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after = os.path.getsize(self.path)
        return before, after

    def backfill_diag_columns(self, chunk: int = 10_000, progress=None) -> int:
        """Fill the diagnostics columns for rows recorded before they
        existed, from each row's raw JSON, in id-ordered chunks so the
        poller's inserts only ever wait one chunk. Idempotent and
        resumable; returns rows touched. Runs once — a settings flag
        records completion."""
        if self.get_setting(DIAG_BACKFILL_SETTING):
            return 0
        touched = 0
        last = int(self.get_setting("diag_backfill_id") or 0)
        while True:
            with self._lock:
                top = self._conn.execute("SELECT MAX(id) FROM vitals_samples").fetchone()[0] or 0
                if last >= top:
                    self._execute("INSERT INTO settings(key, value) VALUES (?, '1') "
                                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                                  (DIAG_BACKFILL_SETTING,))
                    self._execute("DELETE FROM settings WHERE key = 'diag_backfill_id'")
                    self._diag_backfilled = True
                    return touched
                upto = min(top, last + chunk)
                cur = self._execute(
                    """UPDATE vitals_samples SET
                           pilot_high_v = json_extract(raw, '$.pilot_high_v'),
                           pilot_low_v  = json_extract(raw, '$.pilot_low_v'),
                           prox_v       = json_extract(raw, '$.prox_v'),
                           relay_k1_v   = COALESCE(json_extract(raw, '$.relay_k1_v'),
                                                   json_extract(raw, '$.relay_coil_v')),
                           relay_k2_v   = json_extract(raw, '$.relay_k2_v')
                       WHERE id > ? AND id <= ? AND prox_v IS NULL AND raw != ''""",
                    (last, upto),
                )
                touched += cur.rowcount
                last = upto
                self._execute("INSERT INTO settings(key, value) VALUES ('diag_backfill_id', ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(last),))
            if progress:
                progress(last, top)

    def close(self) -> None:
        """Close the shared connection. Owner-only: called once at process
        shutdown (__main__, after every writer has stopped) and by test
        fixtures — nothing else should ever call it."""
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute one write statement and commit immediately.

        The caller must already hold self._lock. Committing per statement
        means every insert is durable before its method returns — there is
        no batching layer to lose samples in."""
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur

    # ---------- writes ----------

    def insert_vitals(self, ts: float, raw: dict, session_id: int | None, total_power_w: float | None) -> int:
        """Store one vitals poll: extracted columns (via VITALS_COLUMNS) plus
        the complete raw JSON. Returns the new row id (the SSE frame carries
        it so clients can de-duplicate)."""
        cols = {col: raw.get(json_key) for col, json_key in VITALS_COLUMNS.items()}
        if cols["relay_k1_v"] is None:
            cols["relay_k1_v"] = raw.get("relay_coil_v")  # older firmware's name for it
        with self._lock:
            cur = self._execute(
                f"""INSERT INTO vitals_samples
                    (ts, session_id, total_power_w, raw, {", ".join(cols)})
                    VALUES (?, ?, ?, ?, {", ".join("?" for _ in cols)})""",
                (ts, session_id, total_power_w, json.dumps(raw), *cols.values()),
            )
            return cur.lastrowid or 0

    def insert_wifi(self, ts: float, raw: dict, ssid: str | None = None) -> int:
        """Store a Wi-Fi status sample. Pass ssid to override the column with
        a decoded value (firmware reports it base64-encoded); the raw JSON
        keeps the original either way."""
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
        """Append to the event log. detail is stored as JSON text; an empty
        dict stores NULL. Writers: the poller (via _event), the web layer's
        amp-controller ingest, and startup/shutdown bookkeeping."""
        with self._lock:
            cur = self._execute(
                "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                (ts, kind, json.dumps(detail) if detail else None),
            )
            return cur.lastrowid or 0

    def start_session(self, start_ts: float) -> int:
        """Open a charging session (plug-in detected). start_ts may predate
        'now': the poller backdates it from the charger's own session timer
        when monitoring begins mid-session. Aggregates stay NULL until
        close_session computes them."""
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
        """Newest session with no end_ts, if any — how a restart discovers a
        session the previous run left open."""
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
        """Deactivate an alert. Returns True only if an active row was
        actually cleared — callers use that edge to fire *_cleared events
        exactly once instead of on every poll."""
        with self._lock:
            cur = self._execute(
                "UPDATE alerts SET active = 0, cleared_ts = ?, last_ts = ? WHERE alert = ? AND source = ? AND active = 1",
                (ts, ts, alert, source),
            )
            return cur.rowcount > 0

    def insert_forecast(self, ts: float, out: dict, session_id: int | None = None) -> int:
        """Store one computed derate forecast (the poller's 30 s tick while
        charging) — the same values the amp controller acts on. Extracted
        columns plus the complete payload as raw JSON, like every other
        sample table."""
        forecast = out.get("forecast") or {}
        model = out.get("model") or {}
        will_trip = forecast.get("will_trip")
        with self._lock:
            cur = self._execute(
                """INSERT INTO forecast_samples
                   (ts, session_id, basis, steady_state_c, will_trip, minutes_to_trip,
                    trip_ts, suggested_max_a, handle_c, current_a, tau_min, fit_rmse_c, trip_c, raw)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    session_id,
                    forecast.get("basis"),
                    forecast.get("steady_state_c"),
                    None if will_trip is None else int(will_trip),
                    forecast.get("minutes_to_trip"),
                    forecast.get("trip_ts"),
                    forecast.get("suggested_max_a"),
                    out.get("handle_c"),
                    out.get("current_a"),
                    model.get("tau_min"),
                    model.get("fit_rmse_c"),
                    model.get("trip_c"),
                    json.dumps(out),
                ),
            )
            return cur.lastrowid or 0

    def forecast_range(self, t_from: float, t_to: float, limit: int = 2000) -> list[dict]:
        """Forecast snapshots in a window, oldest first, columns only (the
        raw payload stays queryable but is not shipped to the chart)."""
        return self._rows(
            """SELECT ts, session_id, basis, steady_state_c, will_trip, minutes_to_trip,
                      trip_ts, suggested_max_a, handle_c, current_a, tau_min, fit_rmse_c, trip_c
               FROM forecast_samples WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?""",
            (t_from, t_to, limit),
        )

    def insert_ambient(self, ts: float, temp_c: float, humidity_pct: float | None = None,
                       pressure_hpa: float | None = None, raw: dict | None = None,
                       source: str | None = None) -> int:
        with self._lock:
            cur = self._execute(
                "INSERT INTO ambient_samples (ts, temp_c, humidity_pct, pressure_hpa, raw, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, temp_c, humidity_pct, pressure_hpa, json.dumps(raw) if raw else None, source),
            )
            return cur.lastrowid

    def ambient_range(self, t_from: float, t_to: float, limit: int = 5000) -> list[dict]:
        """Ambient samples in a window, oldest first. Caveat: a window with
        more rows than limit keeps the *oldest* rows and silently drops the
        newest (ORDER BY ts + LIMIT) — harmless at the 1/min sensor cadence
        (5000 ≈ 3.5 days) but wrong for a naive "latest" query; use
        latest_ambient for that."""
        return self._rows(
            "SELECT ts, temp_c, humidity_pct, pressure_hpa, source FROM ambient_samples "
            "WHERE ts >= ? AND ts <= ? ORDER BY ts LIMIT ?",
            (t_from, t_to, limit),
        )

    def latest_ambient(self) -> dict | None:
        """Newest ambient row regardless of source — no stationary-beats-car
        tiering here; callers that need the tier use thermal's readers."""
        rows = self._rows(
            "SELECT ts, temp_c, humidity_pct, pressure_hpa, source FROM ambient_samples "
            "ORDER BY ts DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def ambient_series(self, t_from: float, t_to: float, exclude_source: str | None = None) -> list[tuple[float, float]]:
        """(ts, temp_c) for every ambient sample in the window, oldest
        first, unbucketed and unlimited — calibration needs the real
        series. exclude_source drops one tag (the car's roaming sensor)."""
        from .calibration import AMBIENT_SQL

        rows = self._rows(AMBIENT_SQL, (t_from, t_to, exclude_source if exclude_source is not None else ""))
        return [(row["ts"], row["temp_c"]) for row in rows]

    def idle_calibration_rows(self, t_from: float, t_to: float) -> list[dict]:
        """Raw (unbucketed) vitals for the idle-offset calibration, in the
        exact shape contrib/calibrate_idle_offset.py reads — same SQL, so
        the two can never disagree. Callers chunk by day."""
        from .calibration import VITALS_SQL

        return self._rows(VITALS_SQL, (t_from, t_to))

    def set_setting(self, key: str, value: str) -> None:
        """Upsert into the string key/value settings table (currently only
        the thermal baseline anchor lives here)."""
        with self._lock:
            self._execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def get_setting(self, key: str) -> str | None:
        rows = self._rows("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def delete_setting(self, key: str) -> None:
        with self._lock:
            self._execute("DELETE FROM settings WHERE key = ?", (key,))

    def active_alerts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alerts WHERE active = 1 ORDER BY first_ts DESC"
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------- reads ----------

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        """Run a read query under the lock, returning plain dicts — detached
        from the cursor and directly JSON-serializable by the web layer."""
        with self._lock:
            return [dict(row) for row in self._conn.execute(sql, params).fetchall()]

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
        sample_count = self._rows(
            "SELECT COUNT(*) AS n FROM vitals_samples WHERE ts >= ? AND ts <= ?", (t_from, t_to)
        )[0]["n"]
        # The device reports 255 (0xFF) for a temperature when the sensor read
        # is momentarily invalid (seen on the handle during connector state
        # transitions). Raw JSON keeps the sentinel; interpreted queries
        # return NULL instead so charts and averages are never poisoned.
        temp = "CASE WHEN {col} >= 255 THEN NULL ELSE {col} END"
        t_pcba, t_handle, t_mcu = (
            temp.format(col=col) for col in ("pcba_temp_c", "handle_temp_c", "mcu_temp_c")
        )
        # Handshake diagnostics live only in the raw JSON; json_extract makes
        # them chartable retroactively for every sample ever recorded.
        # Diagnostics are columns now; the json_extract fallback only runs
        # for rows the one-time backfill hasn't reached (COALESCE stops at
        # the first non-NULL, so backfilled rows never touch the JSON).
        if self._diag_backfilled:
            d = {col: col for col in VITALS_LATER_COLUMNS}
        else:
            # CASE-guarded: a retention-trimmed row has raw = '' and
            # json_extract('') is a hard error, not NULL — and SQLite does
            # not promise to short-circuit COALESCE past it even when the
            # column is populated.
            j = "CASE WHEN raw = '' THEN NULL ELSE json_extract(raw, '{key}') END"
            d = {
                "pilot_high_v": f"COALESCE(pilot_high_v, {j.format(key='$.pilot_high_v')})",
                "pilot_low_v": f"COALESCE(pilot_low_v, {j.format(key='$.pilot_low_v')})",
                "prox_v": f"COALESCE(prox_v, {j.format(key='$.prox_v')})",
                "relay_k1_v": f"COALESCE(relay_k1_v, {j.format(key='$.relay_k1_v')}, "
                              f"{j.format(key='$.relay_coil_v')})",
                "relay_k2_v": f"COALESCE(relay_k2_v, {j.format(key='$.relay_k2_v')})",
            }
        diag = ", ".join(f"{expr} AS {col}" for col, expr in d.items())
        if sample_count <= max_points:
            return _round_rows(self._rows(
                f"""SELECT ts, total_power_w, vehicle_current_a, current_a_a, current_b_a, current_c_a,
                          voltage_a_v, voltage_b_v, voltage_c_v, grid_v, grid_hz,
                          {t_pcba} AS pcba_temp_c, {t_handle} AS handle_temp_c, {t_mcu} AS mcu_temp_c,
                          session_energy_wh,
                          vehicle_connected, contactor_closed, evse_state, session_id,
                          {diag}
                   FROM vitals_samples WHERE ts >= ? AND ts <= ? ORDER BY ts""",
                (t_from, t_to),
            ))
        width = (t_to - t_from) / max_points
        return _round_rows(self._rows(
            f"""SELECT MIN(ts) AS ts, AVG(total_power_w) AS total_power_w, MAX(total_power_w) AS max_power_w,
                      AVG(vehicle_current_a) AS vehicle_current_a,
                      AVG(current_a_a) AS current_a_a, AVG(current_b_a) AS current_b_a,
                      AVG(current_c_a) AS current_c_a,
                      AVG(voltage_a_v) AS voltage_a_v, AVG(voltage_b_v) AS voltage_b_v,
                      AVG(voltage_c_v) AS voltage_c_v,
                      AVG(grid_v) AS grid_v, AVG(grid_hz) AS grid_hz,
                      AVG({t_pcba}) AS pcba_temp_c, AVG({t_handle}) AS handle_temp_c,
                      AVG({t_mcu}) AS mcu_temp_c,
                      MAX(session_energy_wh) AS session_energy_wh,
                      MAX(vehicle_connected) AS vehicle_connected,
                      MAX(contactor_closed) AS contactor_closed,
                      MAX(evse_state) AS evse_state, MAX(session_id) AS session_id,
                      {", ".join(f"AVG({expr}) AS {col}" for col, expr in d.items())}
               FROM vitals_samples WHERE ts >= ? AND ts <= ?
               GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY ts""",
            (t_from, t_to, t_from, width),
        ))

    def lifetime_range(self, t_from: float, t_to: float, max_points: int = 3000) -> list[dict]:
        """Lifetime counter samples in a range (counters are monotonic, so
        buckets keep the last/max value)."""
        sample_count = self._rows(
            "SELECT COUNT(*) AS n FROM lifetime_samples WHERE ts >= ? AND ts <= ?", (t_from, t_to)
        )[0]["n"]
        if sample_count <= max_points:
            return self._rows(
                """SELECT ts, energy_wh, charge_starts, charging_time_s
                   FROM lifetime_samples WHERE ts >= ? AND ts <= ? ORDER BY ts""",
                (t_from, t_to),
            )
        width = (t_to - t_from) / max_points
        return self._rows(
            """SELECT MAX(ts) AS ts, MAX(energy_wh) AS energy_wh,
                      MAX(charge_starts) AS charge_starts, MAX(charging_time_s) AS charging_time_s
               FROM lifetime_samples WHERE ts >= ? AND ts <= ?
               GROUP BY CAST((ts - ?) / ? AS INTEGER) ORDER BY ts""",
            (t_from, t_to, t_from, width),
        )

    def wifi_range(self, t_from: float, t_to: float, max_points: int = 1000) -> list[dict]:
        """Wi-Fi samples in a range, bucket-averaged down to max_points.
        Buckets take MIN(connected)/MIN(internet) so a dropout anywhere in a
        bucket stays visible instead of averaging away."""
        sample_count = self._rows(
            "SELECT COUNT(*) AS n FROM wifi_samples WHERE ts >= ? AND ts <= ?", (t_from, t_to)
        )[0]["n"]
        if sample_count <= max_points:
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
        """Sessions overlapping [t_from, t_to], newest first. A still-open
        session (end_ts NULL) is treated as extending to t_to, so a live
        session always shows in a "recent" query."""
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
        """Alerts whose active span [first_ts, last_ts] overlaps the window,
        active ones first, then newest first."""
        return self._rows(
            """SELECT * FROM alerts WHERE last_ts >= ? AND first_ts <= ?
               ORDER BY active DESC, first_ts DESC""",
            (t_from, t_to),
        )

    def events_range(self, t_from: float, t_to: float, kinds: list[str] | None = None, limit: int = 2000) -> list[dict]:
        """Events in a window, newest first, optionally filtered to specific
        kinds. Unlike ambient_range, the limit here keeps the *newest* rows
        (DESC order), which is what a timeline wants."""
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
        """Row counts for the dashboard footer ("N vitals samples · …")."""
        return {
            "vitals_samples": self._rows("SELECT COUNT(*) AS n FROM vitals_samples")[0]["n"],
            "wifi_samples": self._rows("SELECT COUNT(*) AS n FROM wifi_samples")[0]["n"],
            "lifetime_samples": self._rows("SELECT COUNT(*) AS n FROM lifetime_samples")[0]["n"],
            "sessions": self._rows("SELECT COUNT(*) AS n FROM sessions")[0]["n"],
            "events": self._rows("SELECT COUNT(*) AS n FROM events")[0]["n"],
            "alerts": self._rows("SELECT COUNT(*) AS n FROM alerts")[0]["n"],
        }
