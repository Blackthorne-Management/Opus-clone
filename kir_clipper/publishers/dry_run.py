from __future__ import annotations

import logging
import uuid
from datetime import datetime
from pathlib import Path

from .base import PostStatus

log = logging.getLogger("kir.publish.dry_run")


class DryRunPublisher:
    """Logs what would be posted and posts nothing. Used while `review.dry_run` is on."""

    name = "dry_run"

    def schedule(self, clip_path: Path, caption: str, platforms: list[str], publish_at: datetime | None,
                 title: str | None = None, extra: dict | None = None) -> str:
        job = f"dry-{uuid.uuid4().hex[:10]}"
        log.info("[DRY RUN] would schedule %s to %s at %s | title=%r caption=%r extra=%s", Path(clip_path).name,
                 ",".join(platforms), publish_at or "now", title, caption[:80], extra or {})
        return job

    def status(self, post_id: str) -> PostStatus:
        return PostStatus(state="posted", platform_post_id=post_id)
