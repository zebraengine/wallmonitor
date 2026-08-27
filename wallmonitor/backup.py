"""One-shot database backup: consistent snapshot, verified, compressed,
rotated — into a directory the user chooses.

Where that directory *is* — a local path, a mounted NAS, a folder a sync
client watches (iCloud Drive, Sync, Syncthing, Dropbox), or somewhere a
second machine pulls from with rsync — is deliberately none of this
module's business. wallmonitor never talks to a cloud; it writes a file.

Order of operations, and why:

1. Snapshot with SQLite's online backup API into a temp file *next to the
   source* (never in the destination: a sync-watched folder would upload a
   transient uncompressed copy). Consistent under WAL while the service
   keeps polling — measured at ~4 s for a 1.4 GB database.
2. ``PRAGMA integrity_check`` on the snapshot before compressing. A backup
   that is corrupt but looks fine is the worst outcome; better to fail loud.
3. Compress (gzip by default: ~12x, seconds; xz: ~22x, minutes) to a temp
   name in the destination, fsync, atomic rename — a sync client never sees
   a half-written file.
4. Rotate: keep the newest per day / per ISO week / per month for the
   configured counts, judged by the date in the filename (sync clients
   rewrite mtimes). Only files matching this module's own naming pattern
   are ever deleted; anything else in the folder is left alone.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import lzma
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass

COMPRESSIONS = ("gzip", "xz", "none")
_SUFFIX = {"gzip": ".db.gz", "xz": ".db.xz", "none": ".db"}
_NAME_RE = re.compile(
    r"^wallmonitor-(?P<serial>[A-Za-z0-9_-]+)-(?P<date>\d{8})T(?P<time>\d{6})Z\.db(?:\.gz|\.xz)?$"
)


@dataclass(frozen=True)
class Keep:
    """Rotation policy: how many daily / weekly / monthly snapshots to keep."""

    daily: int = 7
    weekly: int = 4
    monthly: int = 12

    @classmethod
    def parse(cls, spec: str) -> "Keep":
        try:
            d, w, m = (int(part) for part in spec.split("/"))
        except ValueError:
            raise ValueError(f"--backup-keep must be DAILY/WEEKLY/MONTHLY, e.g. 7/4/12 (got {spec!r})")
        if min(d, w, m) < 0:
            raise ValueError("--backup-keep counts must be non-negative")
        return cls(d, w, m)


@dataclass(frozen=True)
class BackupResult:
    path: str
    serial: str
    snapshot_bytes: int
    output_bytes: int
    snapshot_s: float
    compress_s: float
    integrity: str
    deleted: tuple[str, ...]


def _serial(db_path: str) -> str:
    """The pinned charger serial, so multi-charger installs (or two boxes
    sharing one folder) never collide on a filename; 'unpinned' before the
    monitor has ever reached its charger."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'device_serial'").fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        conn.close()
    serial = (row[0] if row else "") or "unpinned"
    return re.sub(r"[^A-Za-z0-9_-]", "_", serial)


def snapshot(db_path: str, dest_path: str) -> tuple[float, str]:
    """Online-backup the live database to dest_path and integrity-check the
    copy. Returns (seconds, integrity result — 'ok' or the first problem)."""
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    t0 = time.monotonic()
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    took = time.monotonic() - t0
    check = sqlite3.connect(f"file:{dest_path}?mode=ro", uri=True)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    return took, integrity


def _compress(src_path: str, out_path: str, compression: str) -> None:
    opener = {
        "gzip": lambda p: gzip.open(p, "wb", compresslevel=6),
        "xz": lambda p: lzma.open(p, "wb", preset=6),
        "none": lambda p: open(p, "wb"),
    }[compression]
    with open(src_path, "rb") as fin, opener(out_path) as fout:
        shutil.copyfileobj(fin, fout, 1 << 20)
        fout.flush()
        os.fsync(fout.fileno())


def _parse(name: str) -> tuple[str, _dt.datetime] | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    when = _dt.datetime.strptime(m["date"] + m["time"], "%Y%m%d%H%M%S").replace(tzinfo=_dt.timezone.utc)
    return m["serial"], when


def rotate(names: list[str], serial: str, keep: Keep) -> list[str]:
    """Which of these filenames to delete for this serial under the policy.
    Pure: judged by the timestamp in each name, so sync clients rewriting
    mtimes can't confuse it. Newest-per-bucket survives in each tier; a
    file kept by any tier is kept. Foreign files are never returned."""
    dated = []
    for name in names:
        parsed = _parse(name)
        if parsed and parsed[0] == serial:
            dated.append((parsed[1], name))
    dated.sort(reverse=True)  # newest first
    if keep.daily == keep.weekly == keep.monthly == 0:
        return []  # 0/0/0: rotate nothing, keep everything
    survivors: set[str] = set()

    def keep_newest_per(bucket, limit: int) -> None:
        seen: list = []
        for when, name in dated:
            key = bucket(when)
            if key in seen:
                continue
            if len(seen) >= limit:
                break
            seen.append(key)
            survivors.add(name)

    keep_newest_per(lambda w: w.date(), keep.daily)
    keep_newest_per(lambda w: w.isocalendar()[:2], keep.weekly)
    keep_newest_per(lambda w: (w.year, w.month), keep.monthly)
    return [name for _, name in dated if name not in survivors]


def run_backup(db_path: str, dest_dir: str, compression: str = "gzip", keep: Keep = Keep(),
               now: float | None = None) -> BackupResult:
    """Snapshot → verify → compress → atomic place → rotate. Raises on any
    failure before the destination is touched; a failed integrity check
    raises RuntimeError with SQLite's first complaint."""
    if compression not in COMPRESSIONS:
        raise ValueError(f"compression must be one of {COMPRESSIONS}")
    os.makedirs(dest_dir, exist_ok=True)
    serial = _serial(db_path)
    stamp = _dt.datetime.fromtimestamp(now if now is not None else time.time(), _dt.timezone.utc)
    final_name = f"wallmonitor-{serial}-{stamp:%Y%m%dT%H%M%SZ}{_SUFFIX[compression]}"
    final_path = os.path.join(dest_dir, final_name)

    # Snapshot beside the source, never in the destination.
    src_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    fd, snap_path = tempfile.mkstemp(prefix=".wallmonitor-snapshot-", suffix=".db", dir=src_dir)
    os.close(fd)
    os.remove(snap_path)  # sqlite creates it; mkstemp only reserved the name
    tmp_out = final_path + ".tmp"
    try:
        snapshot_s, integrity = snapshot(db_path, snap_path)
        if integrity != "ok":
            raise RuntimeError(f"snapshot failed integrity_check: {integrity}")
        snapshot_bytes = os.path.getsize(snap_path)
        t0 = time.monotonic()
        _compress(snap_path, tmp_out, compression)
        compress_s = time.monotonic() - t0
        os.replace(tmp_out, final_path)
        dir_fd = os.open(dest_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass  # some filesystems (network mounts) refuse; the rename already landed
        finally:
            os.close(dir_fd)
    finally:
        # The snapshot inherits WAL mode from the source, so verifying it
        # leaves -wal/-shm siblings behind; sweep those too.
        for path in (snap_path, snap_path + "-journal", snap_path + "-wal", snap_path + "-shm", tmp_out):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    doomed = rotate(os.listdir(dest_dir), serial, keep)
    for name in doomed:
        os.remove(os.path.join(dest_dir, name))
    return BackupResult(
        path=final_path,
        serial=serial,
        snapshot_bytes=snapshot_bytes,
        output_bytes=os.path.getsize(final_path),
        snapshot_s=snapshot_s,
        compress_s=compress_s,
        integrity=integrity,
        deleted=tuple(sorted(doomed)),
    )
