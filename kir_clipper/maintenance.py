"""Keeping the app healthy: heartbeats for the dashboard status bar, daily database backups, disk space."""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path

from . import db
from .config import Config

log = logging.getLogger("kir.maintenance")

STALE_AFTER = {"watcher": timedelta(minutes=5), "publisher": timedelta(minutes=10), "longform": timedelta(minutes=10)}


def beat(conn, name: str, **info) -> None:
    """Record that a background worker is alive (shown in the dashboard status bar)."""
    conn.execute("INSERT INTO heartbeats(name, at, info) VALUES (?,?,?) "
                 "ON CONFLICT(name) DO UPDATE SET at=excluded.at, info=excluded.info",
                 (name, db.iso(db.utcnow()), json.dumps(info, default=str)))


def service_status(conn) -> dict:
    now = db.utcnow()
    out = {}
    for r in conn.execute("SELECT * FROM heartbeats"):
        age = now - db.parse_iso(r["at"])
        out[r["name"]] = {"at": r["at"], "ok": age < STALE_AFTER.get(r["name"], timedelta(minutes=10)),
                          **json.loads(r["info"] or "{}")}
    out["running"] = bool(out.get("watcher", {}).get("ok"))
    return out


def lan_ip() -> str:
    """This PC's address on the home network (what a phone on the same Wi-Fi connects to)."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))   # no packet is sent; just picks the outgoing interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def dashboard_url(cfg: Config, page: str = "") -> str:
    """Link for notifications: the phone address when phone access is on, otherwise this PC."""
    port = int(cfg.get("dashboard.port", 8765))
    host = lan_ip() if cfg.get("dashboard.lan_access") and str(cfg.get("dashboard.pin") or "").isdigit() else "127.0.0.1"
    return f"http://{host}:{port}/" + (f"#{page}" if page else "")


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def backup_db(cfg: Config, keep: int = 14) -> Path:
    """Consistent copy of the SQLite database (safe while the app is running), keeping the last `keep`."""
    folder = cfg.subdir("backups")
    dest = folder / f"kir_clipper-{db.utcnow().strftime('%Y%m%d-%H%M')}.db"
    src = sqlite3.connect(str(cfg.db_path))
    out = sqlite3.connect(str(dest))
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    for old in sorted(folder.glob("kir_clipper-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    log.info("Database backed up to %s", dest.name)
    return dest


def daily_checks(cfg: Config) -> list[str]:
    """Backup + disk check. Returns warnings for the daily summary."""
    warnings = []
    try:
        backup_db(cfg, int(cfg.get("maintenance.keep_backups", 14)))
    except Exception as exc:
        log.exception("Backup failed")
        warnings.append(f"Database backup failed: {exc}")
    floor = float(cfg.get("maintenance.min_free_gb", 20))
    for key in ("data", "processed"):
        gb = free_gb(cfg.path(key))
        if gb < floor:
            warnings.append(f"Low disk space: {gb:.0f} GB free where {key} files are stored ({cfg.path(key)}). "
                            "Move old recordings out of Streams\\processed or free up space; renders fail when the disk fills.")
    return warnings
