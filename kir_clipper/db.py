"""SQLite clip database: streams, clips, posts, performance, plus an action log."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,          -- current location (processed/ after success)
    original_path TEXT NOT NULL,      -- where it was found in the inbox
    hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'stream',  -- stream | import (a reel you posted before using the app)
    duration REAL,
    status TEXT NOT NULL DEFAULT 'new',   -- new | processing | done | failed
    error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    parent_clip_id INTEGER REFERENCES clips(id),  -- set on 90-day re-edits
    kind TEXT NOT NULL DEFAULT 'short',           -- short | longform
    version INTEGER NOT NULL DEFAULT 1,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    score INTEGER NOT NULL,
    hook TEXT,
    reason TEXT,
    title TEXT,
    caption TEXT,
    hashtags TEXT,                    -- JSON list
    platform_captions TEXT,           -- JSON {platform: {title, caption}}
    recipe_json TEXT,
    file_path TEXT,
    status TEXT NOT NULL,             -- see STATUSES
    note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,
    publish_at TEXT NOT NULL,         -- UTC ISO
    posted_at TEXT,
    platform_post_id TEXT,
    request_id TEXT,                  -- posting-service job id
    status TEXT NOT NULL,             -- planned | submitted | manual | posted | failed | cancelled
    step TEXT,                        -- cadence step: trial | feed | mirrored_trial | repost_90 | ...
    trial INTEGER NOT NULL DEFAULT 0, -- 1 = Instagram Trial Reel (shown to non-followers first)
    file_path TEXT,                   -- overrides the clip file (e.g. the mirrored version)
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    id INTEGER PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES posts(id),
    views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
    watch_time REAL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS longform (
    id INTEGER PRIMARY KEY,
    stream_id INTEGER NOT NULL REFERENCES streams(id),
    idx INTEGER NOT NULL,             -- segment number within the stream (1, 2, ...)
    title TEXT,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    status TEXT NOT NULL,             -- needs_segments | queued | planning | rendering | ready | failed
    replan INTEGER NOT NULL DEFAULT 1,
    plan_json TEXT,
    clip_id INTEGER REFERENCES clips(id),  -- the rendered video, posted through the normal queue
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeats (
    name TEXT PRIMARY KEY,            -- watcher | publisher | longform
    at TEXT NOT NULL,
    info TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    ref TEXT,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_platform_time ON posts(platform, publish_at);
"""

# Clip lifecycle
#   dropped   -> score below auto_reject_floor (logged, never queued)
#   review    -> rendered, waiting for owner approval
#   approved  -> approved (manually or by score gate), posts being planned
#   scheduled -> every platform post handed to the posting service
#   posted    -> at least one platform confirmed live
#   rejected  -> owner rejected
#   dry_run   -> would have auto-posted, but dry-run mode is on
#   failed    -> render failed
CLIP_STATUSES = ("pending", "dropped", "review", "approved", "scheduled", "posted", "rejected", "dry_run", "failed")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by older versions up to date."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(clips)")}
    if cols and "kind" not in cols:
        conn.execute("ALTER TABLE clips ADD COLUMN kind TEXT NOT NULL DEFAULT 'short'")
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(streams)")}
    if scols and "kind" not in scols:
        conn.execute("ALTER TABLE streams ADD COLUMN kind TEXT NOT NULL DEFAULT 'stream'")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(posts)")}
    for name, ddl in (("step", "TEXT"), ("trial", "INTEGER NOT NULL DEFAULT 0"), ("file_path", "TEXT")):
        if pcols and name not in pcols:
            conn.execute(f"ALTER TABLE posts ADD COLUMN {name} {ddl}")


def log_event(conn: sqlite3.Connection, kind: str, ref: Any = None, detail: Any = None) -> None:
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, default=str)
    conn.execute(
        "INSERT INTO events(at, kind, ref, detail) VALUES (?,?,?,?)",
        (iso(utcnow()), kind, None if ref is None else str(ref), detail),
    )


# ---- streams ---------------------------------------------------------------

def stream_by_hash(conn, file_hash: str):
    return conn.execute("SELECT * FROM streams WHERE hash=?", (file_hash,)).fetchone()


def insert_stream(conn, path: Path, file_hash: str) -> int:
    cur = conn.execute(
        "INSERT INTO streams(file_path, original_path, hash, status, created_at) VALUES (?,?,?,?,?)",
        (str(path), str(path), file_hash, "processing", iso(utcnow())),
    )
    return cur.lastrowid


def update_stream(conn, stream_id: int, **fields) -> None:
    _update(conn, "streams", stream_id, fields)


# ---- clips -----------------------------------------------------------------

def insert_clip(conn, stream_id: int, clip: dict, status: str, recipe: dict | None = None,
                parent_clip_id: int | None = None, version: int = 1, kind: str = "short") -> int:
    now = iso(utcnow())
    cur = conn.execute(
        """INSERT INTO clips(stream_id, parent_clip_id, version, kind, start, "end", score, hook, reason, title,
               caption, hashtags, platform_captions, recipe_json, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            stream_id, parent_clip_id, version, kind, clip["start"], clip["end"], int(clip["score"]),
            clip.get("hook"), clip.get("reason"), clip.get("title"), clip.get("caption"),
            json.dumps(clip.get("hashtags") or []), json.dumps(clip.get("platform_captions") or {}),
            json.dumps(recipe) if recipe else None, status, now, now,
        ),
    )
    return cur.lastrowid


def update_clip(conn, clip_id: int, **fields) -> None:
    for key in ("hashtags", "platform_captions", "recipe_json"):
        if key in fields and not isinstance(fields[key], (str, type(None))):
            fields[key] = json.dumps(fields[key])
    fields["updated_at"] = iso(utcnow())
    _update(conn, "clips", clip_id, fields)


def get_clip(conn, clip_id: int):
    return conn.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()


def clip_dict(row) -> dict:
    d = dict(row)
    d["hashtags"] = json.loads(d.get("hashtags") or "[]")
    d["platform_captions"] = json.loads(d.get("platform_captions") or "{}")
    d["recipe"] = json.loads(d["recipe_json"]) if d.get("recipe_json") else None
    return d


def clips_with_status(conn, statuses: Iterable[str], kind: str = "short"):
    statuses = list(statuses)
    marks = ",".join("?" * len(statuses))
    return conn.execute(f"SELECT * FROM clips WHERE kind=? AND status IN ({marks}) ORDER BY score DESC, id",
                        (kind, *statuses)).fetchall()


# ---- long-form ---------------------------------------------------------------

def update_longform(conn, lf_id: int, **fields) -> None:
    if "plan_json" in fields and not isinstance(fields["plan_json"], (str, type(None))):
        fields["plan_json"] = json.dumps(fields["plan_json"])
    fields["updated_at"] = iso(utcnow())
    _update(conn, "longform", lf_id, fields)


# ---- posts -----------------------------------------------------------------

def insert_post(conn, clip_id: int, platform: str, publish_at: datetime, step: str | None = None,
                trial: bool = False, file_path: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO posts(clip_id, platform, publish_at, status, step, trial, file_path, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (clip_id, platform, iso(publish_at), "planned", step, int(bool(trial)), file_path, iso(utcnow())),
    )
    return cur.lastrowid


def update_post(conn, post_id: int, **fields) -> None:
    _update(conn, "posts", post_id, fields)


def _update(conn, table: str, row_id: int, fields: dict) -> None:
    if not fields:
        return
    cols = ", ".join(f'"{k}"=?' for k in fields)
    conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*fields.values(), row_id))
