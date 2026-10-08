from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class PublishError(RuntimeError):
    """A post could not be submitted. `retryable` False means retrying will not help (bad auth, bad input)."""

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PostStatus:
    state: str                      # pending | posted | failed
    platform_post_id: str | None = None
    url: str | None = None
    error: str | None = None


class Publisher(Protocol):
    name: str

    def schedule(self, clip_path: Path, caption: str, platforms: list[str], publish_at: datetime | None,
                 title: str | None = None) -> str:
        """Hand a clip to the service. `publish_at=None` means post now. Returns a job/request id."""

    def status(self, post_id: str) -> PostStatus:
        """Look up a previously scheduled job."""
