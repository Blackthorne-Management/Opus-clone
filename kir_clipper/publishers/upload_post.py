"""Upload-Post adapter (https://www.upload-post.com).

Endpoints and field names follow Upload-Post's published examples:
  POST {base_url}/upload                         multipart: user, platform[], video, title, scheduled_date,
                                                 timezone, async_upload (+ youtube_title / youtube_description)
  GET  {base_url}/uploadposts/status?request_id= (or job_id=) for async / scheduled jobs
  Header: Authorization: Apikey <UPLOAD_POST_API_KEY>
Verify these against the live API reference before going live; base_url is configurable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from ..config import Config, secret
from .base import PostStatus, PublishError

log = logging.getLogger("kir.publish.upload_post")


class UploadPostPublisher:
    name = "upload_post"

    def __init__(self, cfg: Config, client: httpx.Client | None = None):
        key = secret("UPLOAD_POST_API_KEY")
        if not key:
            raise PublishError("UPLOAD_POST_API_KEY is not set (add it to .env)", retryable=False)
        up = cfg.get("publishing.upload_post") or {}
        self.base = up.get("base_url", "https://api.upload-post.com/api").rstrip("/")
        self.user = up.get("user")
        self.tz = up.get("timezone")
        if not self.user:
            raise PublishError("publishing.upload_post.user (your Upload-Post profile) is not set", retryable=False)
        self.client = client or httpx.Client(timeout=httpx.Timeout(600, connect=30))
        self._headers = {"Authorization": f"Apikey {key}"}

    def schedule(self, clip_path: Path, caption: str, platforms: list[str], publish_at: datetime | None,
                 title: str | None = None, extra: dict | None = None) -> str:
        data: dict[str, str | list[str]] = {"user": self.user, "title": caption, "async_upload": "true",
                                            "platform[]": list(platforms)}
        if "youtube" in platforms:
            data.update(youtube_title=(title or caption)[:100], youtube_description=caption)
        for key, value in (extra or {}).items():
            data[str(key)] = str(value)
        if publish_at is not None:
            # Send local wall-clock time plus the IANA zone, so there is no ambiguity about offsets.
            if self.tz:
                local = publish_at.astimezone(ZoneInfo(self.tz)).replace(tzinfo=None)
                data["scheduled_date"] = local.isoformat(timespec="seconds")
                data["timezone"] = self.tz
            else:
                data["scheduled_date"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            with open(clip_path, "rb") as fh:
                resp = self.client.post(f"{self.base}/upload", headers=self._headers, data=data,
                                        files={"video": (Path(clip_path).name, fh, "video/mp4")})
        except httpx.HTTPError as exc:
            raise PublishError(f"network error: {type(exc).__name__}") from exc
        body = _json(resp)
        if resp.status_code in (401, 403):
            raise PublishError(f"Upload-Post rejected credentials ({resp.status_code})", retryable=False)
        if resp.status_code == 400:
            raise PublishError(f"Upload-Post rejected the request: {_msg(body)}", retryable=False)
        if resp.status_code >= 300 or body.get("success") is False:
            raise PublishError(f"Upload-Post error {resp.status_code}: {_msg(body)}")
        job = body.get("request_id") or body.get("job_id") or body.get("id")
        if not job:
            # Synchronous success without a job id: treat as posted immediately.
            return "sync:" + ",".join(platforms)
        return str(job)

    def status(self, post_id: str) -> PostStatus:
        if post_id.startswith("sync:"):
            return PostStatus(state="posted")
        key = "job_id" if post_id.startswith("job") else "request_id"
        try:
            resp = self.client.get(f"{self.base}/uploadposts/status", headers=self._headers, params={key: post_id})
        except httpx.HTTPError as exc:
            return PostStatus(state="pending", error=f"network error: {type(exc).__name__}")
        body = _json(resp)
        if resp.status_code == 404:
            return PostStatus(state="pending", error="not found yet")
        if resp.status_code >= 300:
            return PostStatus(state="pending", error=f"status check {resp.status_code}")
        return parse_status(body)


    def metrics(self, request_id: str, platform: str) -> dict | None:
        """Per-post numbers from GET /uploadposts/post-analytics/{request_id}. None if not available yet."""
        if not request_id or request_id.startswith(("sync:", "dry-")):
            return None
        try:
            resp = self.client.get(f"{self.base}/uploadposts/post-analytics/{request_id}", headers=self._headers)
        except httpx.HTTPError:
            return None
        if resp.status_code == 429:
            raise PublishError("analytics rate limit", retryable=True)
        if resp.status_code >= 300:
            return None
        return parse_metrics(_json(resp), platform)


METRIC_KEYS = {
    "views": ("views", "view_count", "video_views", "plays", "play_count", "video_view_count"),
    "likes": ("likes", "like_count", "likes_count", "reactions", "favorite_count"),
    "comments": ("comments", "comment_count", "comments_count", "replies"),
    "shares": ("shares", "share_count", "shares_count", "reposts", "retweet_count"),
    "saves": ("saves", "saved", "save_count", "bookmarks", "bookmark_count"),
    "reach": ("reach", "accounts_reached", "unique_viewers"),
    "watch_time": ("total_time_watched", "watch_time", "estimated_minutes_watched", "estimatedMinutesWatched"),
}


def _find_numbers(node, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k).lower()
            for metric, aliases in METRIC_KEYS.items():
                if key in [a.lower() for a in aliases] and metric not in out:
                    try:
                        out[metric] = float(v["value"] if isinstance(v, dict) and "value" in v else v)
                    except (TypeError, ValueError):
                        pass
            if isinstance(v, (dict, list)):
                _find_numbers(v, out)
    elif isinstance(node, list):
        for v in node:
            _find_numbers(v, out)


def parse_metrics(body: dict, platform: str) -> dict | None:
    """Pull view/like/etc. counts out of the analytics payload, preferring the block for `platform`."""
    scope = body
    for key in ("platforms", "results", "data", "analytics"):
        block = body.get(key) if isinstance(body, dict) else None
        if isinstance(block, dict) and platform in block:
            scope = block[platform]
            break
        if isinstance(block, list):
            match = [b for b in block if isinstance(b, dict) and str(b.get("platform", "")).lower() == platform]
            if match:
                scope = match[0]
                break
    found: dict = {}
    _find_numbers(scope.get("post_metrics", scope) if isinstance(scope, dict) else scope, found)
    if not found and scope is not body:
        _find_numbers(scope, found)
    if not found:
        return None
    return {k: (int(v) if k != "watch_time" else v) for k, v in found.items()}


def parse_status(body: dict) -> PostStatus:
    """Normalise Upload-Post's status payload, tolerating the shapes it has used."""
    raw = str(body.get("status") or body.get("state") or "").lower()
    results = body.get("results") or body.get("result") or {}
    if isinstance(results, list):
        results = {str(i): r for i, r in enumerate(results)}
    post_id = url = err = None
    any_ok = any_fail = False
    for r in results.values() if isinstance(results, dict) else []:
        if not isinstance(r, dict):
            continue
        if r.get("success") is True or str(r.get("status", "")).lower() in ("success", "completed", "published"):
            any_ok = True
            post_id = post_id or r.get("post_id") or r.get("publish_id") or r.get("video_id")
            url = url or r.get("url") or r.get("post_url")
        elif r.get("success") is False or r.get("error"):
            any_fail = True
            err = err or str(r.get("error") or r.get("message") or "failed")
    if any_ok or raw in ("completed", "success", "published", "done"):
        return PostStatus(state="posted", platform_post_id=str(post_id) if post_id else None, url=url)
    if any_fail or raw in ("failed", "error"):
        return PostStatus(state="failed", error=err or str(body.get("error") or body.get("message") or "failed"))
    return PostStatus(state="pending")


def _json(resp: httpx.Response) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"message": resp.text[:300]}


def _msg(body: dict) -> str:
    return str(body.get("message") or body.get("error") or body.get("detail") or body)[:300]
