"""Backup: verified snapshot, compression, atomic placement, rotation."""

import datetime as dt
import gzip
import os
import sqlite3
import time

import pytest

from wallmonitor import backup
from wallmonitor.config import parse_args
from wallmonitor.db import Database


@pytest.fixture
def db(tmp_path):
    (tmp_path / "src").mkdir()
    database = Database(str(tmp_path / "src" / "wallmonitor.db"))
    yield database
    database.close()


def _fill(db, n=200):
    base = time.time() - 3600
    for i in range(n):
        db.insert_vitals(base + i * 10, {"handle_temp_c": 30.0 + i * 0.01, "vehicle_current_a": 48.0,
                                         "pilot_high_v": 8.9, "prox_v": 1.5}, 1, 11000.0)


def _stamp(y, m, d, h=3):
    return dt.datetime(y, m, d, h, 30, tzinfo=dt.timezone.utc).timestamp()


def test_backup_snapshot_is_verified_compressed_and_readable(db, tmp_path):
    _fill(db)
    db.set_setting("device_serial", "PGT-123 456")  # odd chars get sanitized in the name
    dest = tmp_path / "dest"
    result = backup.run_backup(db.path, str(dest), "gzip", now=_stamp(2026, 8, 27))
    assert result.integrity == "ok"
    assert os.path.basename(result.path) == "wallmonitor-PGT-123_456-20260827T033000Z.db.gz"
    assert result.output_bytes < result.snapshot_bytes
    assert result.deleted == ()
    # Nothing but the finished file in the destination — no temp, no snapshot.
    assert sorted(os.listdir(dest)) == [os.path.basename(result.path)]
    # And no snapshot left beside the source either.
    assert sorted(os.listdir(os.path.dirname(db.path))) == ["wallmonitor.db", "wallmonitor.db-shm", "wallmonitor.db-wal"] \
        or all(not name.startswith(".wallmonitor-snapshot") for name in os.listdir(os.path.dirname(db.path)))
    # The compressed copy is a complete, consistent database.
    restored = tmp_path / "restored.db"
    with gzip.open(result.path, "rb") as fin, open(restored, "wb") as fout:
        fout.write(fin.read())
    conn = sqlite3.connect(str(restored))
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM vitals_samples").fetchone()[0] == 200
    assert conn.execute("SELECT value FROM settings WHERE key='device_serial'").fetchone()[0] == "PGT-123 456"
    conn.close()
    # The live database kept working throughout (the snapshot was read-only).
    db.insert_vitals(time.time(), {"handle_temp_c": 31.0}, 1, 0.0)


def test_backup_unpinned_serial_and_no_compression(db, tmp_path):
    _fill(db, 5)
    result = backup.run_backup(db.path, str(tmp_path / "dest"), "none", now=_stamp(2026, 1, 2))
    assert os.path.basename(result.path) == "wallmonitor-unpinned-20260102T033000Z.db"
    assert sqlite3.connect(result.path).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_backup_xz(db, tmp_path):
    _fill(db, 5)
    result = backup.run_backup(db.path, str(tmp_path / "dest"), "xz", now=_stamp(2026, 1, 2))
    assert result.path.endswith(".db.xz") and result.output_bytes < result.snapshot_bytes


def test_rotation_keeps_newest_per_bucket_and_leaves_foreign_files():
    # 40 consecutive daily snapshots plus strays: rotation is judged from the
    # timestamp in the name (sync clients rewrite mtimes) and never touches
    # files it didn't write.
    names = []
    for i in range(40):
        when = dt.datetime(2026, 8, 27, 3, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=i)
        names.append(f"wallmonitor-SER1-{when:%Y%m%dT%H%M%SZ}.db.gz")
    names += ["wallmonitor-SER1-20260827T010000Z.db.gz",  # same day, earlier: not the newest of that day
              "wallmonitor-OTHER-20260827T033000Z.db.gz",  # a different charger's file
              "notes.txt", "wallmonitor.db", "wallmonitor-SER1-garbage.db.gz"]
    doomed = set(backup.rotate(names, "SER1", backup.Keep(7, 4, 12)))
    kept = set(names) - doomed
    # Foreign / other-serial / malformed names are never deleted.
    for foreign in ("wallmonitor-OTHER-20260827T033000Z.db.gz", "notes.txt", "wallmonitor.db",
                    "wallmonitor-SER1-garbage.db.gz"):
        assert foreign in kept
    # The 7 newest days survive; the earlier same-day copy does not.
    for i in range(7):
        when = dt.datetime(2026, 8, 27, 3, 30, tzinfo=dt.timezone.utc) - dt.timedelta(days=i)
        assert f"wallmonitor-SER1-{when:%Y%m%dT%H%M%SZ}.db.gz" in kept
    assert "wallmonitor-SER1-20260827T010000Z.db.gz" in doomed
    # Weekly tier: newest per ISO week for 4 weeks; monthly: newest per month.
    survivors = sorted(n for n in kept if n.startswith("wallmonitor-SER1-2026"))
    weeks = {dt.datetime.strptime(n[17:32], "%Y%m%dT%H%M%S").isocalendar()[:2] for n in survivors}
    months = {n[17:23] for n in survivors}
    assert len(weeks) >= 4 and {"202607", "202608"} <= months
    # 40 dailies collapse to well under half.
    assert 7 <= len(survivors) <= 14
    # 0/0/0 keeps everything.
    assert backup.rotate(names, "SER1", backup.Keep(0, 0, 0)) == []


def test_backup_rotation_deletes_on_disk(db, tmp_path):
    _fill(db, 5)
    db.set_setting("device_serial", "SER1")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "keep-me.txt").write_text("not yours")
    for d in range(1, 12):
        backup.run_backup(db.path, str(dest), "none", backup.Keep(3, 0, 0), now=_stamp(2026, 8, d))
    names = sorted(os.listdir(dest))
    assert "keep-me.txt" in names
    snaps = [n for n in names if n.startswith("wallmonitor-SER1-")]
    assert [n[17:25] for n in snaps] == ["20260809", "20260810", "20260811"]


def test_backup_refuses_corrupt_source(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000)
    with pytest.raises(Exception):
        backup.run_backup(str(bad), str(tmp_path / "dest"))
    assert not (tmp_path / "dest").exists() or os.listdir(tmp_path / "dest") == []


def test_backup_flag_needs_no_host(tmp_path):
    cfg = parse_args(["--backup", str(tmp_path / "b"), "--db", str(tmp_path / "x.db")])
    assert cfg.backup_dir == str(tmp_path / "b") and cfg.backup_compress == "gzip"
    with pytest.raises(SystemExit):
        parse_args(["--backup", "x", "--backup-keep", "7/4"])
