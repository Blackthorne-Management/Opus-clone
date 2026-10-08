"""Posting layer. Every service sits behind the `Publisher` interface so it can be swapped."""

from __future__ import annotations

from ..config import Config
from .base import PostStatus, Publisher, PublishError
from .dry_run import DryRunPublisher


def get_publisher(cfg: Config) -> Publisher:
    provider = cfg.get("publishing.provider", "upload_post")
    if provider == "dry_run":
        return DryRunPublisher()
    if provider == "upload_post":
        from .upload_post import UploadPostPublisher

        return UploadPostPublisher(cfg)
    raise ValueError(f"Unknown publishing.provider: {provider}")


__all__ = ["Publisher", "PostStatus", "PublishError", "DryRunPublisher", "get_publisher"]
