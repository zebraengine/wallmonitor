"""The temperature discriminators were validated by running the script against
production data, but the dew-point discriminator cannot be validated that way:
it would take a deliberately mis-sited sensor and a wait for contamination to
show. These tests fabricate the two physical mechanisms instead — charger-style
dry heating, where RH falls exactly enough to hold the dew point flat, and an
air-exchange bump at constant RH, where the dew point inherits the temperature
move — and require the discriminator to tell them apart from *identical*
temperature traces. The temperature-only evidence is the same in both
scenarios by construction; only the humidity channel differs."""

import importlib.util
import math
import pathlib
import sqlite3
import sys
from collections.abc import Sequence

import pytest

spec = importlib.util.spec_from_file_location(
    "check_ambient_contamination",
    pathlib.Path(__file__).parent.parent / "contrib" / "check_ambient_contamination.py",
)
assert spec is not None and spec.loader is not None
cac = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cac  # dataclass annotation resolution needs this
spec.loader.exec_module(cac)

T0 = 1_754_000_000.0
SEGMENT_S = 1800.0


def _rh_for_dew_point(temp_c: float, dew_point_c: float) -> float:
    """Inverse Magnus: the RH at temp_c whose dew point is dew_point_c."""
    b, c = 17.62, 243.12
    gamma = (b * dew_point_c) / (c + dew_point_c)
    return 100.0 * math.exp(gamma - (b * temp_c) / (c + temp_c))


def _handle_c(t: float) -> float:
    """Exponential approach plus a minute-scale wiggle (current steps, fan
    cycles) — the fast structure the detrended tests key on."""
    return 20.0 + 25.0 * (1.0 - math.exp(-t / 500.0)) + 3.0 * math.sin(2 * math.pi * t / 600.0)


def _segment() -> list[dict]:
    return [
        {
            "ts": T0 + t,
            "vehicle_current_a": 32.0,
            "contactor_closed": 1,
            "handle_temp_c": _handle_c(t),
        }
        for t in range(0, int(SEGMENT_S) + 1, 30)
    ]


def _conn_with_ambient(rows: Sequence[tuple[float, float, float | None]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE ambient_samples (
               id INTEGER PRIMARY KEY, ts REAL NOT NULL, temp_c REAL NOT NULL,
               humidity_pct REAL, pressure_hpa REAL, source TEXT, raw TEXT)"""
    )
    conn.executemany(
        "INSERT INTO ambient_samples (ts, temp_c, humidity_pct, source) VALUES (?, ?, ?, 'ecowitt')",
        rows,
    )
    return conn


def _coupled_ambient(t: float) -> float:
    """A contaminated reading: slow diurnal ramp plus a share of the handle's
    rise, wiggle included."""
    return 25.0 + 0.0006 * t + 0.15 * (_handle_c(t) - 20.0)


def test_dew_point_magnus():
    assert cac._dew_point_c(20.0, 100.0) == pytest.approx(20.0, abs=0.02)
    assert cac._dew_point_c(25.0, 50.0) == pytest.approx(13.9, abs=0.2)
    for bad in (None, 0.0, -5.0, 150.0):
        assert cac._dew_point_c(25.0, bad) is None


def test_dry_heating_holds_dew_point_flat():
    # RH falls exactly enough to pin the dew point near 15 C (small jitter at a
    # period incommensurate with the handle wiggle, so zero variance doesn't
    # void the correlation) — the charger adds heat, never moisture.
    rows = []
    for t in range(0, int(SEGMENT_S) + 1, 60):
        temp = _coupled_ambient(t)
        dew = 15.0 + 0.05 * math.sin(2 * math.pi * t / 1234.0 + 1.0)
        rows.append((T0 + t, temp, _rh_for_dew_point(temp, dew)))
    res = cac.analyse(_conn_with_ambient(rows), _segment(), "ecowitt")

    assert res is not None
    assert res.detrended_r is not None and res.detrended_r > 0.5
    assert res.dewpoint_r is not None and abs(res.dewpoint_r) < 0.3


def test_arrived_air_carries_dew_point_along():
    # Same temperature trace, but at constant RH — the bump is air that brought
    # its own moisture, so the dew point tracks the handle just like temperature.
    rows = [(T0 + t, _coupled_ambient(t), 55.0) for t in range(0, int(SEGMENT_S) + 1, 60)]
    res = cac.analyse(_conn_with_ambient(rows), _segment(), "ecowitt")

    assert res is not None
    assert res.detrended_r is not None and res.detrended_r > 0.5
    assert res.dewpoint_r is not None and res.dewpoint_r > 0.5


def test_missing_humidity_degrades_to_temperature_only():
    rows = [(T0 + t, _coupled_ambient(t), None) for t in range(0, int(SEGMENT_S) + 1, 60)]
    res = cac.analyse(_conn_with_ambient(rows), _segment(), "ecowitt")

    assert res is not None
    assert res.detrended_r is not None and res.detrended_r > 0.5
    assert res.dewpoint_r is None
