"""Owner notifications: always logged to data/notifications.log, optionally sent to a webhook."""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx

from . import db
from .config import Config, secret
from .logging_setup import redact

log = logging.getLogger("kir.notify")


def notify(cfg: Config, subject: str, body: str, level: str = "info") -> None:
    text = redact(f"[KIR clipper] {subject}\n{body}".strip())
    getattr(log, "warning" if level == "alert" else "info")(text.replace("\n", " | "))
    with open(cfg.data_dir / "notifications.log", "a", encoding="utf-8") as fh:
        fh.write(f"{db.iso(db.utcnow())} [{level}] {text}\n\n")
    url = secret("ALERT_WEBHOOK_URL")
    if url:
        try:
            # "content" for Discord, "text" for Slack-compatible hooks.
            httpx.post(url, json={"content": text[:1900], "text": text[:3900]}, timeout=15)
        except httpx.HTTPError as exc:
            log.warning("Could not deliver notification webhook: %s", type(exc).__name__)


def daily_summary(conn, cfg: Config) -> str:
    now = db.utcnow()
    since, until = db.iso(now - timedelta(days=1)), db.iso(now + timedelta(days=1))
    posted = conn.execute(
        """SELECT p.platform, c.title, c.score FROM posts p JOIN clips c ON c.id=p.clip_id
           WHERE p.status='posted' AND p.posted_at >= ? ORDER BY p.posted_at""", (since,)).fetchall()
    upcoming = conn.execute(
        """SELECT p.platform, p.publish_at, c.title FROM posts p JOIN clips c ON c.id=p.clip_id
           WHERE p.status IN ('planned','submitted') AND p.publish_at <= ? ORDER BY p.publish_at""", (until,)).fetchall()
    waiting = conn.execute("SELECT COUNT(*) FROM clips WHERE status='review'").fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM posts WHERE status='failed' AND created_at >= ?",
                          (db.iso(now - timedelta(days=7)),)).fetchone()[0]
    lines = [f"Posted in the last 24h: {len(posted)}"]
    lines += [f"  - {r['platform']}: {r['title']} (score {r['score']})" for r in posted]
    lines.append(f"Scheduled for the next 24h: {len(upcoming)}")
    lines += [f"  - {r['platform']} at {r['publish_at']}: {r['title']}" for r in upcoming]
    lines.append(f"Waiting for your approval: {waiting}")
    if failed:
        lines.append(f"Failed posts in the last 7 days: {failed} (see dashboard)")
    if cfg.dry_run:
        lines.append("DRY RUN is on: nothing is actually being posted.")
    return "\n".join(lines)
