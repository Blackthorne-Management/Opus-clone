from datetime import datetime, timezone

import httpx
import pytest

from kir_clipper.publishers import PublishError
from kir_clipper.publishers.upload_post import UploadPostPublisher, parse_status


def make(cfg, monkeypatch, handler):
    monkeypatch.setenv("UPLOAD_POST_API_KEY", "test-upload-key-123456")
    return UploadPostPublisher(cfg, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_schedule_sends_expected_fields(cfg, monkeypatch, tmp_path):
    seen = {}

    def handler(req: httpx.Request):
        seen["auth"] = req.headers["authorization"]
        seen["url"] = str(req.url)
        seen["body"] = req.content.decode("latin-1")
        return httpx.Response(200, json={"success": True, "request_id": "req-1"})

    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"\x00video")
    pub = make(cfg, monkeypatch, handler)
    job = pub.schedule(clip, "my caption #kir", ["youtube"], datetime(2026, 10, 9, 15, 30, tzinfo=timezone.utc), title="YT title")
    assert job == "req-1"
    assert seen["auth"] == "Apikey test-upload-key-123456"
    assert seen["url"].endswith("/api/upload")
    for needle in ('name="platform[]"', "youtube", 'name="scheduled_date"', "2026-10-09T11:30:00", "America/New_York",
                   'name="youtube_title"', "YT title", 'name="video"; filename="c.mp4"', 'name="user"'):
        assert needle in seen["body"]


@pytest.mark.parametrize("code,retryable", [(401, False), (400, False), (500, True), (429, True)])
def test_error_mapping(cfg, monkeypatch, tmp_path, code, retryable):
    clip = tmp_path / "c.mp4"
    clip.write_bytes(b"x")
    pub = make(cfg, monkeypatch, lambda req: httpx.Response(code, json={"message": "nope"}))
    with pytest.raises(PublishError) as exc:
        pub.schedule(clip, "c", ["tiktok"], None)
    assert exc.value.retryable is retryable


def test_parse_status_shapes():
    assert parse_status({"status": "completed", "results": {"tiktok": {"success": True, "url": "u"}}}).state == "posted"
    assert parse_status({"results": [{"success": False, "error": "quota"}]}).error == "quota"
    assert parse_status({"status": "pending"}).state == "pending"
