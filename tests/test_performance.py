from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from kir_clipper import db
from kir_clipper.performance import due_posts, fetch_due, snapshot
from kir_clipper.publishers import PublishError
from kir_clipper.publishers.upload_post import parse_metrics
from kir_clipper.web.app import create_app

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def posted(conn, tmp_path, days_ago, platform="instagram", request_id="req1"):
    sid = db.insert_stream(conn, tmp_path / "s.mp4", f"h{days_ago}{platform}{request_id}")
    cid = db.insert_clip(conn, sid, {"start": 0, "end": 30, "score": 90}, "posted")
    pid = db.insert_post(conn, cid, platform, NOW - timedelta(days=days_ago), step="feed")
    db.update_post(conn, pid, status="posted", posted_at=db.iso(NOW - timedelta(days=days_ago)), request_id=request_id)
    return pid


def test_parse_metrics_shapes():
    by_platform = {"success": True, "platforms": {"instagram": {"post_metrics": {"views": 1520, "likes": 88,
                   "comments": 4, "shares": 9, "saved": 12, "reach": 1300}}, "tiktok": {"post_metrics": {"views": 9}}}}
    assert parse_metrics(by_platform, "instagram") == {"views": 1520, "likes": 88, "comments": 4, "shares": 9,
                                                       "saves": 12, "reach": 1300}
    as_list = {"results": [{"platform": "tiktok", "metrics": {"play_count": 5000, "like_count": {"value": 300},
                                                             "total_time_watched": 812.5}}]}
    assert parse_metrics(as_list, "tiktok") == {"views": 5000, "likes": 300, "watch_time": 812.5}
    assert parse_metrics({"status": "pending"}, "youtube") is None


def test_checkpoints_and_fetch(cfg, conn, tmp_path):
    young = posted(conn, tmp_path, 0.5, request_id="r-young")
    day2 = posted(conn, tmp_path, 2, request_id="r-day2")
    by_hand = posted(conn, tmp_path, 4, request_id=None)
    assert {r["id"] for r in due_posts(conn, NOW)} == {day2, by_hand}

    class Pub:
        calls = []

        def metrics(self, request_id, platform):
            self.calls.append(request_id)
            return {"views": 1000, "likes": 50}

    pub = Pub()
    assert fetch_due(conn, cfg, pub, now=NOW) == 1                   # hand-posted one is skipped
    assert pub.calls == ["r-day2"]
    assert fetch_due(conn, cfg, pub, now=NOW + timedelta(hours=1)) == 0      # already has its day-1 snapshot
    # a day later: the 2-day-old post hits its day-3 checkpoint and the young one its day-1 checkpoint
    assert fetch_due(conn, cfg, pub, now=NOW + timedelta(days=1, hours=1)) == 2
    assert snapshot(conn, day2, days=3)["views"] == 1000
    assert young not in {r["id"] for r in due_posts(conn, NOW)}


def test_rate_limit_stops_the_run(cfg, conn, tmp_path):
    posted(conn, tmp_path, 2, request_id="a")
    posted(conn, tmp_path, 3, request_id="b")

    class Limited:
        def metrics(self, request_id, platform):
            raise PublishError("analytics rate limit")

    assert fetch_due(conn, cfg, Limited(), now=NOW) == 0


def test_numbers_can_be_entered_by_hand(cfg, conn, tmp_path):
    pid = posted(conn, tmp_path, 3, request_id=None)
    client = TestClient(create_app(cfg))
    assert client.post(f"/api/posts/{pid}/metrics", json={"views": "12,400", "likes": "900"}).status_code == 200
    assert client.post(f"/api/posts/{pid}/metrics", json={"views": "lots"}).status_code == 400
    row = conn.execute("SELECT views, likes, source FROM performance WHERE post_id=?", (pid,)).fetchone()
    assert tuple(row) == (12400, 900, "manual")
    posts = client.get("/api/posts").json()
    assert next(p for p in posts if p["id"] == pid)["views"] == 12400
